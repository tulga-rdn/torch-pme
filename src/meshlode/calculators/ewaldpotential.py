from typing import List, Optional, Union

import torch

from ..lib import generate_kvectors_squeezed
from .base import CalculatorBaseTorch, _ShortRange


class _EwaldPotentialImpl(_ShortRange):
    def __init__(
        self,
        exponent: float,
        atomic_smearing: Union[None, float],
        lr_wavelength: Union[None, float],
        subtract_self: bool,
        subtract_interior: bool,
    ):
        if exponent < 0.0 or exponent > 3.0:
            raise ValueError(f"`exponent` p={exponent} has to satisfy 0 < p < 3")
        if atomic_smearing is not None and atomic_smearing <= 0:
            raise ValueError(f"`atomic_smearing` {atomic_smearing} has to be positive")

        _ShortRange.__init__(
            self, exponent=exponent, subtract_interior=subtract_interior
        )
        self.atomic_smearing = atomic_smearing
        self.lr_wavelength = lr_wavelength

        # If interior contributions are to be subtracted, also do so for self term
        if self.subtract_interior:
            subtract_self = True
        self.subtract_self = subtract_self

    def _compute_single_system(
        self,
        positions: torch.Tensor,
        charges: torch.Tensor,
        cell: torch.Tensor,
        neighbor_indices: torch.Tensor,
        neighbor_shifts: torch.Tensor,
    ) -> torch.Tensor:
        # Set the defaut values of convergence parameters
        # The total computational cost = cost of SR part + cost of LR part
        # Bigger smearing increases the cost of the SR part while decreasing the cost
        # of the LR part. Since the latter usually is more expensive, we maximize the
        # value of the smearing by default to minimize the cost of the LR part.
        # The auxilary parameter lr_wavelength then control the
        # convergence of the SR and LR sums, respectively. The default values are
        # chosen to reach a convergence on the order of 1e-4 to 1e-5 for the test
        # structures.
        if self.atomic_smearing is None:
            cell_dimensions = torch.linalg.norm(cell, dim=1)
            max_cutoff = torch.min(cell_dimensions) / 2 - 1e-6
            smearing = max_cutoff.item() / 5.0
        else:
            smearing = self.atomic_smearing

        if self.lr_wavelength is None:
            lr_wavelength = 0.5 * smearing
        else:
            lr_wavelength = self.lr_wavelength

        # Compute short-range (SR) part using a real space sum
        potential_sr = self._compute_sr(
            positions=positions,
            charges=charges,
            cell=cell,
            smearing=smearing,
            neighbor_indices=neighbor_indices,
            neighbor_shifts=neighbor_shifts,
        )

        # Compute long-range (LR) part using a Fourier / reciprocal space sum
        potential_lr = self._compute_lr(
            positions=positions,
            charges=charges,
            cell=cell,
            smearing=smearing,
            lr_wavelength=lr_wavelength,
        )

        potential_ewald = potential_sr + potential_lr
        return potential_ewald

    def _compute_lr(
        self,
        positions: torch.Tensor,
        charges: torch.Tensor,
        cell: torch.Tensor,
        smearing: torch.Tensor,
        lr_wavelength: torch.Tensor,
        subtract_self=True,
    ) -> torch.Tensor:
        # Define k-space cutoff from required real-space resolution
        k_cutoff = 2 * torch.pi / lr_wavelength

        # Compute number of times each basis vector of the reciprocal space can be
        # scaled until the cutoff is reached
        basis_norms = torch.linalg.norm(cell, dim=1)
        ns_float = k_cutoff * basis_norms / 2 / torch.pi
        ns = torch.ceil(ns_float).long()

        # Generate k-vectors and evaluate
        # kvectors = self._generate_kvectors(ns=ns, cell=cell)
        kvectors = generate_kvectors_squeezed(ns=ns, cell=cell)
        knorm_sq = torch.sum(kvectors**2, dim=1)

        # G(k) is the Fourier transform of the Coulomb potential
        # generated by a Gaussian charge density
        # We remove the singularity at k=0 by explicitly setting its
        # value to be equal to zero. This mathematically corresponds
        # to the requirement that the net charge of the cell is zero.
        # G = 4 * torch.pi * torch.exp(-0.5 * smearing**2 * knorm_sq) / knorm_sq
        G = self.potential.potential_fourier_from_k_sq(knorm_sq, smearing)
        G[0] = self.potential.potential_fourier_at_zero(smearing)

        # Compute the energy using the explicit method that
        # follows directly from the Poisson summation formula.
        # For this, we precompute trigonometric factors for optimization, which leads
        # to N^2 rather than N^3 scaling.
        trig_args = kvectors @ (positions.T)  # shape num_k x num_atoms

        # Reshape charges into suitable form for array/tensor broadcasting
        num_atoms = len(positions)
        if charges.dim() > 1:
            num_channels = charges.shape[1]
            charges_reshaped = (charges.T).reshape(num_channels, 1, num_atoms)
            sum_idx = 2
        else:
            charges_reshaped = charges
            sum_idx = 1

        # Actual computation of trigonometric factors
        cos_all = torch.cos(trig_args)
        sin_all = torch.sin(trig_args)
        cos_summed = torch.sum(cos_all * charges_reshaped, dim=sum_idx)
        sin_summed = torch.sum(sin_all * charges_reshaped, dim=sum_idx)

        # Add up the contributions to compute the potential
        energy = torch.zeros_like(charges)
        for i in range(num_atoms):
            energy[i] += torch.sum(
                G * cos_all[:, i] * cos_summed, dim=sum_idx - 1
            ) + torch.sum(G * sin_all[:, i] * sin_summed, dim=sum_idx - 1)
        energy /= torch.abs(cell.det())

        # Remove self contribution if desired
        # For now, this is the expression for the Coulomb potential p=1
        # TODO: modify to expression for general p
        if subtract_self:
            self_contrib = (
                torch.sqrt(torch.tensor(2.0 / torch.pi, device=positions.device))
                / smearing
            )
            energy -= charges * self_contrib

        return energy


class EwaldPotential(CalculatorBaseTorch, _EwaldPotentialImpl):
    r"""Specie-wise long-range potential computed using the Ewald sum.

    Scaling as :math:`\mathcal{O}(N^2)` with respect to the number of particles
    :math:`N`.

    For computing a **neighborlist** a reasonable ``cutoff`` is half the length of
    the shortest cell vector, which can be for example computed according as

    .. code-block:: python

        cell_dimensions = torch.linalg.norm(cell, dim=1)
        cutoff = torch.min(cell_dimensions) / 2 - 1e-6

    This ensures a accuracy of the short range part of ``1e-5``.

    :param exponent: the exponent :math:`p` in :math:`1/r^p` potentials
    :param atomic_smearing: Width of the atom-centered Gaussian used to split the
        Coulomb potential into the short- and long-range parts. A reasonable value for
        most systems is to set it to ``1/5`` times the neighbor list cutoff. If
        :py:obj:`None` ,it will be set to 1/5 times of half the largest box vector
        (separately for each structure).
    :param lr_wavelength: Spatial resolution used for the long-range (reciprocal space)
        part of the Ewald sum. More conretely, all Fourier space vectors with a
        wavelength >= this value will be kept. If not set to a global value, it will be
        set to half the atomic_smearing parameter to ensure convergence of the
        long-range part to a relative precision of 1e-5.
    :param subtract_self: If set to :py:obj:`True`, subtract from the features of an
        atom the contributions to the potential arising from that atom itself (but not
        the periodic images).
    :param subtract_interior: If set to :py:obj:`True`, subtract from the features of an
        atom the contributions to the potential arising from all atoms within the cutoff
        Note that if set to true, the self contribution (see previous) is also
        subtracted by default.

    For an **example** on the usage refer to :py:class:`PMEPotential`.
    """

    def __init__(
        self,
        exponent: float = 1.0,
        atomic_smearing: Optional[float] = None,
        lr_wavelength: Optional[float] = None,
        subtract_self: bool = True,
        subtract_interior: bool = False,
    ):
        _EwaldPotentialImpl.__init__(
            self,
            exponent=exponent,
            atomic_smearing=atomic_smearing,
            lr_wavelength=lr_wavelength,
            subtract_self=subtract_self,
            subtract_interior=subtract_interior,
        )
        CalculatorBaseTorch.__init__(self)

    def compute(
        self,
        positions: Union[List[torch.Tensor], torch.Tensor],
        charges: Union[List[torch.Tensor], torch.Tensor],
        cell: Union[List[torch.Tensor], torch.Tensor],
        neighbor_indices: Union[List[torch.Tensor], torch.Tensor],
        neighbor_shifts: Union[List[torch.Tensor], torch.Tensor],
    ) -> Union[torch.Tensor, List[torch.Tensor]]:
        """Compute potential for all provided "systems" stacked inside list.

        The computation is performed on the same ``device`` as ``dtype`` is the input is
        stored on. The ``dtype`` of the output tensors will be the same as the input.

        For computing a **neighborlist** a reasonable ``cutoff`` is half the length of
        the shortest cell vector, which can be for example computed according as

        .. code-block:: python

            cell_dimensions = torch.linalg.norm(cell, dim=1)
            cutoff = torch.min(cell_dimensions) / 2 - 1e-6

        :param positions: Single or 2D tensor of shape (``len(charges), 3``) containing
            the Cartesian positions of all point charges in the system.
        :param charges: Single 2D tensor or list of 2D tensor of shape (``n_channels,
            len(positions))``. ``n_channels`` is the number of charge channels the
            potential should be calculated for a standard potential ``n_channels=1``. If
            more than one "channel" is provided multiple potentials for the same
            position but different are computed.
        :param cell: single or 2D tensor of shape (3, 3), describing the bounding
            box/unit cell of the system. Each row should be one of the bounding box
            vector; and columns should contain the x, y, and z components of these
            vectors (i.e. the cell should be given in row-major order).
        :param neighbor_indices: Optional single or list of 2D tensors of shape (2, n),
            where n is the number of atoms. The 2 rows correspond to the indices of
            the two atoms which are considered neighbors (e.g. within a cutoff distance)
        :param neighbor_shifts: Optional single or list of 2D tensors of shape (3, n),
             where n is the number of atoms. The 3 rows correspond to the shift indices
             for periodic images.
        :return: Single or List of torch Tensors containing the potential(s) for all
            positions. Each tensor in the list is of shape ``(len(positions),
            len(charges))``, where If the inputs are only single tensors only a single
            torch tensor with the potentials is returned.
        """

        return self._compute_impl(
            positions=positions,
            charges=charges,
            cell=cell,
            neighbor_indices=neighbor_indices,
            neighbor_shifts=neighbor_shifts,
        )

    # This function is kept to keep MeshLODE compatible with the broader pytorch
    # infrastructure, which require a "forward" function. We name this function
    # "compute" instead, for compatibility with other COSMO software.
    def forward(
        self,
        positions: Union[List[torch.Tensor], torch.Tensor],
        charges: Union[List[torch.Tensor], torch.Tensor],
        cell: Union[List[torch.Tensor], torch.Tensor],
        neighbor_indices: Union[List[torch.Tensor], torch.Tensor] = None,
        neighbor_shifts: Union[List[torch.Tensor], torch.Tensor] = None,
    ) -> Union[torch.Tensor, List[torch.Tensor]]:
        """Forward just calls :py:meth:`compute`."""
        return self.compute(
            positions=positions,
            charges=charges,
            cell=cell,
            neighbor_indices=neighbor_indices,
            neighbor_shifts=neighbor_shifts,
        )
