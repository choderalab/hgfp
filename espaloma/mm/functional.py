# =============================================================================
# IMPORTS
# =============================================================================
import math
import torch
import espaloma as esp

# =============================================================================
# CONSTANTS
# =============================================================================
from simtk import unit
from simtk.unit.quantity import Quantity
LJ_SWITCH = Quantity(
    1.0,
    unit.angstrom).value_in_unit(
        esp.units.DISTANCE_UNIT
    )

# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================
def linear_mixture_to_original(k1, k2, b1, b2):
    """ Translating linear mixture coefficients back to original
    parameterization.
    """
    # (batch_size, )
    k = k1 + k2

    # (batch_size, )
    b = (k1 * b1 + k2 * b2) / (k + 1e-3)

    return k, b


# =============================================================================
# MODULE FUNCTIONS
# =============================================================================
def harmonic(x, k, eq, order=[2]):
    """ Harmonic term.

    Parameters
    ----------
    x : `torch.Tensor`, `shape=(batch_size, 1)`
    k : `torch.Tensor`, `shape=(batch_size, len(order))`
    eq : `torch.Tensor`, `shape=(batch_size, len(order))`
    order : `int` or `List` of `int`

    Returns
    -------
    u : `torch.Tensor`, `shape=(batch_size, 1)`
    """

    if isinstance(order, list):
        order = torch.tensor(order, device=x.device)

    return k * ((x - eq)).pow(order[:, None, None]).permute(1, 2, 0).sum(
        dim=-1
    )


def periodic_fixed_phases(dihedrals: torch.Tensor, ks: torch.Tensor) -> torch.Tensor:
    """Periodic torsion term with n_phases = 6, periodicities = 1..n_phases, phases = zeros

    Parameters
    ----------
    dihedrals : torch.Tensor, shape=(batch_size, n_phases)
        dihedral angles -- TODO: confirm in radians?
    ks : torch.Tensor, shape=(batch_size, n_phases)
        force constants -- TODO: confirm in esp.unit.ENERGY_UNIT ?

    Returns
    -------
    u : torch.Tensor, shape=(batch_size, 1)
        potential energy of each snapshot

    Notes
    -----
    TODO: is there a way to annotate / type-hint tensor shapes? (currently adding assert statements)
    TODO: merge with esp.mm.functional.periodic -- adding this because I was having difficulty debugging runtime tensor
      shape errors in esp.mm.functional.periodic, which allows for a more flexible mix of input shapes and types
    """
    n_phases = 6
    # periodicity ns = 1..n_phases
    assert (dihedrals.shape == ks.shape)
    assert (dihedrals.shape[1] == n_phases)
    batch_size = len(dihedrals)
    ns = torch.arange(n_phases) + 1

    # cos(n * theta) for n in 1..n_phases
    energy_terms = ks * torch.cos(ns * dihedrals)

    # sum over n
    energy_sums = torch.sum(energy_terms, dim=1)
    assert (energy_sums.shape == (batch_size, 1))

    return energy_sums


def periodic(x, k, periodicity=list(range(1, 7)), phases=[0.0 for _ in range(6)]):
    """ Periodic term.

    Parameters
    ----------
    x : `torch.Tensor`, `shape=(batch_size, 1)`
    k : `torch.Tensor`, `shape=(batch_size, number_of_phases)`
    eq: `torch.Tensor`, `shape=(batch_size, number_of_phases)`
    """

    if isinstance(phases, list):
        phases = torch.tensor(phases, device=x.device)

    if isinstance(periodicity, list):
        periodicity = torch.tensor(
            periodicity, device=x.device, dtype=torch.get_default_dtype(),
        )

    if periodicity.ndim == 1:
        periodicity = periodicity[None, None, :].repeat(
            x.shape[0],
            x.shape[1],
            1
        )

    elif periodicity.ndim == 2:
        periodicity = periodicity[:, None, :].repeat(
            1,
            x.shape[1],
            1
        )

    if phases.ndim == 1:
        phases = phases[None, None, :].repeat(
            x.shape[0],
            x.shape[1],
            1,
        )

    elif phases.ndim == 2:
        phases = phases[:, None, :].repeat(
            1,
            x.shape[1],
            1,
        )

    n_theta = periodicity * x[:, :, None]

    n_theta_minus_phases = n_theta - phases

    cos_n_theta_minus_phases = n_theta_minus_phases.cos()

    k = k[:, None, :].repeat(
        1, x.shape[1], 1
    )

    energy = (k * (1.0 + cos_n_theta_minus_phases)).sum(dim=-1)

    return energy

# simple implementation
# def harmonic(x, k, eq):
#     return k * (x - eq) ** 2
#
# def harmonic_re(x, k, eq, a=0.0, b=0.3):
#     # temporary
#     ka = k
#     kb = eq
#
#     c = ((ka * a + kb * b) / (ka + kb)) ** 2 - a ** 2 - b ** 2
#
#     return ka * (x - a) ** 2 + kb * (x - b) ** 2


def lj(x, epsilon, sigma, order=[12, 6], coefficients=[1.0, 1.0], switch=LJ_SWITCH):
    r""" Lennard-Jones term.

    Notes
    -----
    ..math::
    E  = \epsilon  ((\sigma / r) ^ {12} - (\sigma / r) ^ 6)

    Parameters
    ----------
    x : `torch.Tensor`, `shape=(batch_size, 1)`
    epsilon : `torch.Tensor`, `shape=(batch_size, len(order))`
    sigma : `torch.Tensor`, `shape=(batch_size, len(order))`
    order : `int` or `List` of `int`

    Returns
    -------
    u : `torch.Tensor`, `shape=(batch_size, 1)`


    """
    if isinstance(order, list):
        order = torch.tensor(order, device=x.device)

    if isinstance(coefficients, list):
        coefficients = torch.tensor(coefficients, device=x.device)

    assert order.shape[0] == 2
    assert order.dim() == 1

    # TODO:
    # for experiments only
    # erase later

    # compute sigma over x
    sigma_over_x = sigma / x

    # erase values under switch
    sigma_over_x = torch.where(
        torch.lt(x, switch),
        torch.zeros_like(sigma_over_x),
        sigma_over_x,
    )

    return epsilon * (
            coefficients[0] * sigma_over_x ** order[0]
            - coefficients[1] * sigma_over_x ** order[1]
        )

def gaussian(x, coefficients, phases=[idx * 0.001 for idx in range(200)]):
    r""" Gaussian basis function.

    """

    if isinstance(phases, list):
        # (number_of_phases, )
        phases = torch.tensor(phases, device=x.device)

    # broadcasting
    # (number_of_hypernodes, number_of_snapshots, number_of_phases)
    phases = phases[None, None, :].repeat(x.shape[0], x.shape[1], 1)
    x = x[:, :, None].repeat(1, 1, phases.shape[-1])
    coefficients = coefficients[:, None, :].repeat(1, x.shape[1], 1)

    return (coefficients * torch.exp(-0.5 * (x - phases) ** 2)).sum(-1)

def linear_mixture(x, coefficients, phases=[0.10, 0.25]):
    r""" Linear mixture basis function.

    """

    assert len(phases) == 2, 'Only two phases now.'
    assert coefficients.shape[-1] == 2

    # partition the dimensions
    # (, )
    b1 = phases[0]
    b2 = phases[1]

    # (batch_size, 1)
    k1 = coefficients[:, 0][:, None]
    k2 = coefficients[:, 1][:, None]

    # get the original parameters
    # (batch_size, )
    k, b = linear_mixture_to_original(k1, k2, b1, b2)

    # (batch_size, 1)
    u1 = k1 * (x - b1) ** 2
    u2 = k2 * (x - b1) ** 2

    u = u1 + u2 - k1 * b1 ** 2 - k2 ** b2 ** 2 + b ** 2

    return u
