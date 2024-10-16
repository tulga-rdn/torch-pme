from .. import calculators as torch_calculators
from .base import Calculator


class PMECalculator(Calculator):
    r"""
    Potential using a particle mesh-based Ewald (PME).

    Refer to :py:class:`torchpme.PMECalculator` for parameter documentation.

    For an **example** on the usage for any calculator refer to :ref:`userdoc-how-to`.
    """

    # see torchpme.metatensor.base
    _base_calculator = torch_calculators.PMECalculator