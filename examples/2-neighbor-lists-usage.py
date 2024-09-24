"""
Advanced neighbor list usage
============================

Accurately calculating forces as derivatives from energy is crucial for predicting
system dynamics as well as in training machine learning models. In systems where forces
are derived from the gradients of the potential energy, it is essential that the
distance calculations between particles are included in the computational graph. This
ensures that the force computations respect the dependencies between particle positions
and distances, allowing for accurate gradients during backpropagation.

.. figure:: ../../static/images/backprop-path.*

    Visualization of the data flow to compute the energy from the ``cell``,
    ``positions`` and ``charges`` through a neighborlist calculator and the potential
    calculator. All operations on the red line have to be tracked to obtain the correct
    computation of derivatives on the ``positions``.

In this tutorial, we demonstrate two methods for maintaining differentiability when
computing distances between particles. The **first method** manually recomputes
``distances`` within the computational graph using ``positions``, ``cell`` information,
and neighbor shifts, making it suitable for any neighbor list code.

The **second method** uses a backpropagable neighbor list from the `vesin-torch
<https://luthaf.fr/vesin>`_ library, which automatically ensures that the distance
calculations remain differentiable.

.. note::

    While both approaches yield the same result, a backpropagable neighbor list is
    generally preferred because it eliminates the need to manually recompute distances.
    This not only simplifies your code but also improves performance.
"""

# %%
from typing import Optional

import ase
import chemiscope
import matplotlib.pyplot as plt
import numpy as np
import torch
import vesin
import vesin.torch

from torchpme import PMEPotential


# %%
#
# The test system
# ---------------
#
# As a test system, we use a 2x2x2 supercell of an CsCl crystal in a cubic cell.

atoms_unitcell = ase.Atoms(
    symbols=["Cs", "Cl"],
    positions=np.array([(0, 0, 0), (0.5, 0.5, 0.5)]),
    cell=np.eye(3),
    pbc=True,
)
charges_unitcell = np.array([1.0, -1.0])

atoms = atoms_unitcell.repeat([2, 2, 2])
charges = np.tile(charges_unitcell, 2 * 2 * 2)

# %%
#
# We now slightly displace the atoms from their initial positions randomly based on a
# Gaussian distribution with a width of 0.1 Å to create non-zero forces.

rng = np.random.default_rng(42)
displacement = rng.normal(loc=0.0, scale=0.1, size=(len(atoms), 3))
atoms.positions += displacement

chemiscope.show(
    frames=[atoms],
    mode="structure",
    settings=chemiscope.quick_settings(structure_settings={"unitCell": True}),
)

# %%
#
# Generic Neighborlist
# --------------------
#
# One usual workflow is to compute the distance vectors using default tools like the
# the default (NumPy) version of the vesin neighbor list.

nl = vesin.NeighborList(cutoff=1.0, full_list=False)
i, j, S = nl.compute(
    points=atoms.positions, box=atoms.cell.array, periodic=True, quantities="ijS"
)

# %%
#
# We now define a function that (re-)computes the distances in a way that torch can
# track these operations.


def distances(
    positions: torch.Tensor,
    neighbor_indices: torch.Tensor,
    cell: Optional[torch.Tensor] = None,
    neighbor_shifts: Optional[torch.Tensor] = None,
) -> torch.Tensor:
    """Compute pairwise distances."""
    atom_is = neighbor_indices[:, 0]
    atom_js = neighbor_indices[:, 1]

    pos_is = positions[atom_is]
    pos_js = positions[atom_js]

    distance_vectors = pos_js - pos_is

    if cell is not None and neighbor_shifts is not None:
        shifts = neighbor_shifts.type(cell.dtype)
        distance_vectors += shifts @ cell
    elif cell is not None and neighbor_shifts is None:
        raise ValueError("Provided `cell` but no `neighbor_shifts`.")
    elif cell is None and neighbor_shifts is not None:
        raise ValueError("Provided `neighbor_shifts` but no `cell`.")

    return torch.linalg.norm(distance_vectors, dim=1)


# %%
#
# To use this function, we first convert the ``positions``, ``charges`` and the ``cell``
# from NumPu arrays into torch tensors and enable the tracking of operations by setting
# the :py:attr:`requires_grad <torch.Tensor.requires_grad>` property to :py:obj:`True`.

positions = torch.from_numpy(atoms.positions)
charges = torch.from_numpy(charges).unsqueeze(1)
cell = torch.from_numpy(atoms.cell.array)

positions.requires_grad = True

i = torch.from_numpy(i.astype(int))
j = torch.from_numpy(j.astype(int))
neighbor_indices = torch.stack([i, j], dim=1)
neighbor_shifts = torch.from_numpy(S)


# %%
#
# Now, we start to re-compute the distances

neighbor_distances = distances(
    positions=positions,
    neighbor_indices=neighbor_indices,
    cell=cell,
    neighbor_shifts=neighbor_shifts,
)

# %%
#
# and initialize a PME instance to compute the potential.

pme = PMEPotential()
potential = pme(
    positions=positions,
    charges=charges,
    cell=cell,
    neighbor_indices=neighbor_indices,
    neighbor_distances=neighbor_distances,
)

print(potential)

# %%
#
# The energy is given by the scalar product of the potential with the charges.

energy = charges.T @ potential

# %%
#
# Finally, we can compute and print the forces in CGS units as erg/Å.

forces = torch.autograd.grad(-1.0 * energy, positions)[0]

print(forces)

# %%
#
# Backpropagable Neighborlist
# ---------------------------
#
# We now repeat the computation of the forces, but instead of using a generic neighbor
# list and our custom ``distances`` function, we directly use a neighbor list function
# that tracks the operations, as implemented by the ``vesin-torch`` library.
#
# We first ``detach`` and ``clone`` the position tensor to create a new computational
# graph

positions_new = positions.detach().clone()
positions_new.requires_grad = True

# %%
#
# and create new distances in a similar manner as above.

nl = vesin.torch.NeighborList(cutoff=1.0, full_list=False)
i, j, d = nl.compute(points=positions_new, box=cell, periodic=True, quantities="ijd")

neighbor_indices_new = torch.stack([i, j], dim=1)

# %%
#
# Following the same steps as above, we compute the forces.

potential_new = pme(
    positions=positions_new,
    charges=charges,
    cell=cell,
    neighbor_indices=neighbor_indices_new,
    neighbor_distances=d,
)

energy_new = charges.T @ potential_new

forces_new = torch.autograd.grad(-1.0 * energy_new, positions_new)[0]

print(forces_new)

# %%
#
# The forces are the same as those we printed above. For better comparison, we can also
# plot the scalar force for each method.

plt.plot(torch.linalg.norm(forces, dim=1), "o-", label="normal Neighborlist")
plt.plot(torch.linalg.norm(forces_new, dim=1), ".-", label="torch Neighborlist")
plt.legend()

plt.xlabel("atom index")
plt.ylabel(r"$|F|~/~\mathrm{erg\,Å^{-1}}$")

plt.show()