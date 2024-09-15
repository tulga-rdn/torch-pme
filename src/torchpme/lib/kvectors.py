import torch


def _generate_kvectors(
    ns: torch.Tensor, cell: torch.Tensor, for_ewald: bool
) -> torch.Tensor:
    # Check that all provided parameters have the correct shapes and are consistent
    # with each other
    if ns.shape != (3,):
        raise ValueError(f"ns of shape {list(ns.shape)} should be of shape (3, )")

    if cell.shape != (3, 3):
        raise ValueError(f"cell of shape {list(cell.shape)} should be of shape (3, 3)")

    if ns.device != cell.device:
        raise ValueError(
            f"`ns` and `cell` are not on the same device, got {ns.device} and "
            f"{cell.device}."
        )

    if cell.is_cuda:
        # use function that does not synchronize with the CPU
        inverse_cell = torch.linalg.inv_ex(cell)[0]
    else:
        inverse_cell = torch.linalg.inv(cell)

    reciprocal_cell = 2 * torch.pi * inverse_cell.T
    bx = reciprocal_cell[0]
    by = reciprocal_cell[1]
    bz = reciprocal_cell[2]

    # Generate all reciprocal space vectors from real FFT!
    # The frequencies from the fftfreq function  are of the form [0, 1/n, 2/n, ...]
    # These are then converted to [0, 1, 2, ...] by multiplying with n.
    # get the frequencies, multiply with n, then w/ the reciprocal space vectors
    kxs = (bx * ns[0]) * torch.fft.fftfreq(ns[0], device=ns.device).unsqueeze(-1)
    kys = (by * ns[1]) * torch.fft.fftfreq(ns[1], device=ns.device).unsqueeze(-1)

    if for_ewald:
        kzs = (bz * ns[2]) * torch.fft.fftfreq(ns[2], device=ns.device).unsqueeze(-1)
    else:
        kzs = (bz * ns[2]) * torch.fft.rfftfreq(ns[2], device=ns.device).unsqueeze(-1)

    # then take the cartesian product (all possible combinations, same as meshgrid)
    # via broadcasting (to avoid instantiating intermediates), and sum up
    return kxs[:, None, None] + kys[None, :, None] + kzs[None, None, :]


def generate_kvectors_for_mesh(ns: torch.Tensor, cell: torch.Tensor) -> torch.Tensor:
    """Compute all reciprocal space vectors for Fourier space sums.

    This variant is used in combination with **mesh based calculators** using the fast
    fourier transform (FFT) algorithm.

    :param ns: torch.tensor of shape ``(3,)`` and dtype int
        ``ns = [nx, ny, nz]`` contains the number of mesh points in the x-, y- and
        z-direction, respectively. For faster performance during the Fast Fourier
        Transform (FFT) it is recommended to use values of nx, ny and nz that are
        powers of 2.
    :param cell: torch.tensor of shape ``(3, 3)``
        Tensor specifying the real space unit cell of a structure, where ``cell[i]`` is
        the i-th basis vector

    :return: torch.tensor of shape ``(nx, ny, nz, 3)`` containing all reciprocal
        space vectors that will be used in the (FFT-based) mesh calculators.
        Note that ``k_vectors[0,0,0] = [0,0,0]`` always is the zero vector.

    .. seealso::

        :py:func:`generate_kvectors_for_ewald` for a function to be used for Ewald
        calculators.
    """
    return _generate_kvectors(ns=ns, cell=cell, for_ewald=False)


def generate_kvectors_for_ewald(ns: torch.Tensor, cell: torch.Tensor) -> torch.Tensor:
    """Compute all reciprocal space vectors for Fourier space sums.

    This variant is used with the **Ewald calculator**, in which the sum over the
    reciprocal space vectors is performed explicitly rather than using the fast Fourier
    transform (FFT) algorithm.

    The main difference with :py:func:`generate_kvectors_for_mesh` is the shape of the
    output tensor (see documentation on return) and the fact that the full set of
    reciprocal space vectors is returned, rather than the FFT-optimized set that roughly
    contains only half of the vectors.

    :param ns: torch.tensor of shape ``(3,)`` and dtype int
        ``ns = [nx, ny, nz]`` contains the number of mesh points in the x-, y- and
        z-direction, respectively.
    :param cell: torch.tensor of shape ``(3, 3)``
        Tensor specifying the real space unit cell of a structure, where ``cell[i]`` is
        the i-th basis vector

    :return: torch.tensor of shape ``(n, 3)`` containing all reciprocal
        space vectors that will be used in the Ewald calculator.
        Note that ``k_vectors[0] = [0,0,0]`` always is the zero vector.

    .. seealso::

        :py:func:`generate_kvectors_for_mesh` for a function to be used with mmesh based
        calculators.
    """
    return _generate_kvectors(ns=ns, cell=cell, for_ewald=True).reshape(-1, 3)