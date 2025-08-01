# Copyright 2018-2023 Xanadu Quantum Technologies Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Functions to sample a state."""


import numpy as np

import pennylane as qml
from pennylane.measurements import (
    ClassicalShadowMP,
    CountsMP,
    ExpectationMP,
    SampleMeasurement,
    ShadowExpvalMP,
    Shots,
)
from pennylane.ops import LinearCombination, Prod, SProd, Sum
from pennylane.typing import TensorLike

from .apply_operation import apply_operation
from .measure import flatten_state


def jax_random_split(prng_key, num: int = 2):
    """Get a new key with ``jax.random.split``."""
    if prng_key is None:
        return (None,) * num
    # pylint: disable=import-outside-toplevel
    from jax.random import split

    return split(prng_key, num=num)


def _group_measurements(mps: list[SampleMeasurement | ClassicalShadowMP | ShadowExpvalMP]):
    """
    Group the measurements such that:
      - measurements with pauli observables pairwise-commute in each group
      - measurements with observables that are not pauli words are all in different groups
      - measurements without observables are all in the same group
      - classical shadow measurements are all in different groups
    """
    if len(mps) == 1:
        return [mps], [[0]]

    # measurements with pauli-word observables
    mp_pauli_obs = []

    # measurements with non pauli-word observables
    mp_other_obs = []
    mp_other_obs_indices = []

    # measurements with no observables
    mp_no_obs = []
    mp_no_obs_indices = []
    for i, mp in enumerate(mps):
        if isinstance(mp.obs, (Sum, SProd, Prod)):
            mps[i].obs = qml.simplify(mp.obs)
        if isinstance(mp, (ClassicalShadowMP, ShadowExpvalMP)):
            mp_other_obs.append([mp])
            mp_other_obs_indices.append([i])
        elif mp.obs is None:
            mp_no_obs.append(mp)
            mp_no_obs_indices.append(i)
        elif qml.pauli.is_pauli_word(mp.obs):
            mp_pauli_obs.append((i, mp))
        else:
            mp_other_obs.append([mp])
            mp_other_obs_indices.append([i])
    if mp_pauli_obs:
        i_to_pauli_mp = dict(mp_pauli_obs)
        part_indices = qml.pauli.compute_partition_indices(
            [mp.obs for mp in i_to_pauli_mp.values()]
        )
        coeffs = list(i_to_pauli_mp.keys())
        group_indices = [[coeffs[idx] for idx in group] for group in part_indices]
        mp_pauli_groups = []
        for indices in group_indices:
            mp_group = [i_to_pauli_mp[i] for i in indices]
            mp_pauli_groups.append(mp_group)
    else:
        mp_pauli_groups, group_indices = [], []

    mp_no_obs_indices = [mp_no_obs_indices] if mp_no_obs else []
    mp_no_obs = [mp_no_obs] if mp_no_obs else []
    all_mp_groups = mp_pauli_groups + mp_no_obs + mp_other_obs
    all_indices = group_indices + mp_no_obs_indices + mp_other_obs_indices

    return all_mp_groups, all_indices


def _get_num_executions_for_expval_H(obs):
    indices = obs.grouping_indices
    if indices:
        return len(indices)
    return _get_num_wire_groups_for_expval_H(obs)


def _get_num_wire_groups_for_expval_H(obs):
    _, obs_list = obs.terms()
    wires_list = []
    added_obs = []
    num_groups = 0
    for o in obs_list:
        if o in added_obs:
            continue
        if isinstance(o, qml.Identity):
            continue
        added = False
        for wires in wires_list:
            if len(qml.wires.Wires.shared_wires([wires, o.wires])) == 0:
                added_obs.append(o)
                added = True
                break
        if not added:
            added_obs.append(o)
            wires_list.append(o.wires)
            num_groups += 1
    return num_groups


def _get_num_executions_for_sum(obs):

    if obs.grouping_indices:
        return len(obs.grouping_indices)

    if not obs.pauli_rep:
        return sum(int(not isinstance(o, qml.Identity)) for o in obs.terms()[1])

    _, ops = obs.terms()
    with qml.QueuingManager.stop_recording():
        op_groups = qml.pauli.group_observables(ops)
    return len(op_groups)


def get_num_shots_and_executions(tape: qml.tape.QuantumScript) -> tuple[int, int]:
    """Get the total number of qpu executions and shots.

    Args:
        tape (qml.tape.QuantumTape): the tape we want to get the number of executions and shots for

    Returns:
        int, int: the total number of QPU executions and the total number of shots

    """
    groups, _ = _group_measurements(tape.measurements)

    num_executions = 0
    num_shots = 0
    for group in groups:
        if isinstance(group[0], ExpectationMP) and isinstance(
            group[0].obs, qml.ops.LinearCombination
        ):
            H_executions = _get_num_executions_for_expval_H(group[0].obs)
            num_executions += H_executions
            if tape.shots:
                num_shots += tape.shots.total_shots * H_executions
        elif isinstance(group[0], ExpectationMP) and isinstance(group[0].obs, qml.ops.Sum):
            sum_executions = _get_num_executions_for_sum(group[0].obs)
            num_executions += sum_executions
            if tape.shots:
                num_shots += tape.shots.total_shots * sum_executions
        elif isinstance(group[0], (ClassicalShadowMP, ShadowExpvalMP)):
            num_executions += tape.shots.total_shots
            if tape.shots:
                num_shots += tape.shots.total_shots
        else:
            num_executions += 1
            if tape.shots:
                num_shots += tape.shots.total_shots

    if tape.batch_size:
        num_executions *= tape.batch_size
        if tape.shots:
            num_shots *= tape.batch_size
    return num_executions, num_shots


def _apply_diagonalizing_gates(
    mps: list[SampleMeasurement], state: np.ndarray, is_state_batched: bool = False
):
    if len(mps) == 1:
        diagonalizing_gates = mps[0].diagonalizing_gates()
    elif all(mp.obs for mp in mps):
        diagonalizing_gates = qml.pauli.diagonalize_qwc_pauli_words([mp.obs for mp in mps])[0]
    else:
        diagonalizing_gates = []

    for op in diagonalizing_gates:
        state = apply_operation(op, state, is_state_batched=is_state_batched)

    return state


# pylint:disable = too-many-arguments
def measure_with_samples(
    measurements: list[SampleMeasurement | ClassicalShadowMP | ShadowExpvalMP],
    state: np.ndarray,
    shots: Shots,
    is_state_batched: bool = False,
    rng=None,
    prng_key=None,
    mid_measurements: dict = None,
) -> list[TensorLike]:
    """
    Returns the samples of the measurement process performed on the given state.
    This function assumes that the user-defined wire labels in the measurement process
    have already been mapped to integer wires used in the device.

    Args:
        measurements (List[Union[SampleMeasurement, ClassicalShadowMP, ShadowExpvalMP]]):
            The sample measurements to perform
        state (np.ndarray[complex]): The state vector to sample from
        shots (Shots): The number of samples to take
        is_state_batched (bool): whether the state is batched or not
        rng (Union[None, int, array_like[int], SeedSequence, BitGenerator, Generator]): A
            seed-like parameter matching that of ``seed`` for ``numpy.random.default_rng``.
            If no value is provided, a default RNG will be used.
        prng_key (Optional[jax.random.PRNGKey]): An optional ``jax.random.PRNGKey``. This is
            the key to the JAX pseudo random number generator. Only for simulation using JAX.
        mid_measurements (None, dict): Dictionary of mid-circuit measurements

    Returns:
        List[TensorLike[Any]]: Sample measurement results
    """
    # last N measurements are sampling MCMs in ``dynamic_one_shot`` execution mode
    mps = measurements[0 : -len(mid_measurements)] if mid_measurements else measurements

    groups, indices = _group_measurements(mps)
    all_res = []
    for group in groups:
        if isinstance(group[0], ExpectationMP) and isinstance(group[0].obs, LinearCombination):
            measure_fn = _measure_hamiltonian_with_samples
        elif isinstance(group[0], ExpectationMP) and isinstance(group[0].obs, Sum):
            measure_fn = _measure_sum_with_samples
        elif isinstance(group[0], (ClassicalShadowMP, ShadowExpvalMP)):
            measure_fn = _measure_classical_shadow
        else:
            # measure with the usual method (rotate into the measurement basis)
            measure_fn = _measure_with_samples_diagonalizing_gates

        prng_key, key = jax_random_split(prng_key)
        all_res.extend(
            measure_fn(
                group, state, shots, is_state_batched=is_state_batched, rng=rng, prng_key=key
            )
        )

    flat_indices = [_i for i in indices for _i in i]

    # reorder results
    sorted_res = tuple(
        res for _, res in sorted(list(enumerate(all_res)), key=lambda r: flat_indices[r[0]])
    )

    # append MCM samples
    if mid_measurements:
        sorted_res += tuple(mid_measurements.values())

    # put the shot vector axis before the measurement axis
    if shots.has_partitioned_shots:
        sorted_res = tuple(zip(*sorted_res))

    return sorted_res


def _measure_with_samples_diagonalizing_gates(
    mps: list[SampleMeasurement],
    state: np.ndarray,
    shots: Shots,
    is_state_batched: bool = False,
    rng=None,
    prng_key=None,
) -> TensorLike:
    """
    Returns the samples of the measurement process performed on the given state,
    by rotating the state into the measurement basis using the diagonalizing gates
    given by the measurement process.

    Args:
        mp (~.measurements.SampleMeasurement): The sample measurement to perform
        state (np.ndarray[complex]): The state vector to sample from
        shots (~.measurements.Shots): The number of samples to take
        is_state_batched (bool): whether the state is batched or not
        rng (Union[None, int, array_like[int], SeedSequence, BitGenerator, Generator]): A
            seed-like parameter matching that of ``seed`` for ``numpy.random.default_rng``.
            If no value is provided, a default RNG will be used.
        prng_key (Optional[jax.random.PRNGKey]): An optional ``jax.random.PRNGKey``. This is
            the key to the JAX pseudo random number generator. Only for simulation using JAX.

    Returns:
        TensorLike[Any]: Sample measurement results
    """
    # apply diagonalizing gates
    state = _apply_diagonalizing_gates(mps, state, is_state_batched)

    total_indices = len(state.shape) - is_state_batched
    wires = qml.wires.Wires(range(total_indices))

    def _process_single_shot(samples):
        processed = []
        for mp in mps:
            res = mp.process_samples(samples, wires)
            if not isinstance(mp, CountsMP):
                res = qml.math.squeeze(res)

            processed.append(res)

        return tuple(processed)

    try:
        prng_key, _ = jax_random_split(prng_key)
        samples = sample_state(
            state,
            shots=shots.total_shots,
            is_state_batched=is_state_batched,
            wires=wires,
            rng=rng,
            prng_key=prng_key,
        )
    except ValueError as e:
        if "probabilities contain nan" not in str(e).lower():
            raise e
        samples = qml.math.full((shots.total_shots, len(wires)), 0)

    processed_samples = []
    for lower, upper in shots.bins():
        shot = _process_single_shot(samples[..., lower:upper, :])
        processed_samples.append(shot)

    if shots.has_partitioned_shots:
        return tuple(zip(*processed_samples))

    return processed_samples[0]


def _measure_classical_shadow(
    mp: list[ClassicalShadowMP | ShadowExpvalMP],
    state: np.ndarray,
    shots: Shots,
    is_state_batched: bool = False,
    rng=None,
    prng_key=None,
):
    """
    Returns the result of a classical shadow measurement on the given state.

    A classical shadow measurement doesn't fit neatly into the current measurement API
    since different diagonalizing gates are used for each shot. Here it's treated as a
    state measurement with shots instead of a sample measurement.

    Args:
        mp (~.measurements.SampleMeasurement): The sample measurement to perform
        state (np.ndarray[complex]): The state vector to sample from
        shots (~.measurements.Shots): The number of samples to take
        rng (Union[None, int, array_like[int], SeedSequence, BitGenerator, Generator]): A
            seed-like parameter matching that of ``seed`` for ``numpy.random.default_rng``.
            If no value is provided, a default RNG will be used.

    Returns:
        TensorLike[Any]: Sample measurement results
    """
    # pylint: disable=unused-argument

    # the list contains only one element based on how we group measurements
    mp = mp[0]

    wires = qml.wires.Wires(range(len(state.shape)))

    if shots.has_partitioned_shots:
        return [tuple(mp.process_state_with_shots(state, wires, s, rng=rng) for s in shots)]

    return [mp.process_state_with_shots(state, wires, shots.total_shots, rng=rng)]


def _measure_hamiltonian_with_samples(
    mp: list[SampleMeasurement],
    state: np.ndarray,
    shots: Shots,
    is_state_batched: bool = False,
    rng=None,
    prng_key=None,
):
    # the list contains only one element based on how we group measurements
    mp = mp[0]

    # if the measurement process involves a Hamiltonian, measure each
    # of the terms separately and sum
    def _sum_for_single_shot(s, prng_key=None):
        results = measure_with_samples(
            [ExpectationMP(t) for t in mp.obs.terms()[1]],
            state,
            s,
            is_state_batched=is_state_batched,
            rng=rng,
            prng_key=prng_key,
        )
        return sum(c * res for c, res in zip(mp.obs.terms()[0], results))

    keys = jax_random_split(prng_key, num=shots.num_copies)
    unsqueezed_results = tuple(
        _sum_for_single_shot(type(shots)(s), key) for s, key in zip(shots, keys)
    )
    return [unsqueezed_results] if shots.has_partitioned_shots else [unsqueezed_results[0]]


def _measure_sum_with_samples(
    mp: list[SampleMeasurement],
    state: np.ndarray,
    shots: Shots,
    is_state_batched: bool = False,
    rng=None,
    prng_key=None,
):
    # the list contains only one element based on how we group measurements
    mp = mp[0]

    # if the measurement process involves a Sum, measure each
    # of the terms separately and sum
    def _sum_for_single_shot(s, prng_key=None):
        results = measure_with_samples(
            [ExpectationMP(t) for t in mp.obs],
            state,
            s,
            is_state_batched=is_state_batched,
            rng=rng,
            prng_key=prng_key,
        )
        return sum(results)

    keys = jax_random_split(prng_key, num=shots.num_copies)
    unsqueezed_results = tuple(
        _sum_for_single_shot(type(shots)(s), key) for s, key in zip(shots, keys)
    )
    return [unsqueezed_results] if shots.has_partitioned_shots else [unsqueezed_results[0]]


def sample_state(
    state,
    shots: int,
    is_state_batched: bool = False,
    wires=None,
    rng=None,
    prng_key=None,
) -> np.ndarray:
    """
    Returns a series of samples of a state.

    Args:
        state (array[complex]): A state vector to be sampled
        shots (int): The number of samples to take
        is_state_batched (bool): whether the state is batched or not
        wires (Sequence[int]): The wires to sample
        rng (Union[None, int, array_like[int], SeedSequence, BitGenerator, Generator]):
            A seed-like parameter matching that of ``seed`` for ``numpy.random.default_rng``.
            If no value is provided, a default RNG will be used
        prng_key (Optional[jax.random.PRNGKey]): An optional ``jax.random.PRNGKey``. This is
            the key to the JAX pseudo random number generator. Only for simulation using JAX.

    Returns:
        ndarray[int]: Sample values of the shape (shots, num_wires)
    """

    total_indices = len(state.shape) - is_state_batched
    state_wires = qml.wires.Wires(range(total_indices))

    wires_to_sample = wires or state_wires
    num_wires = len(wires_to_sample)

    flat_state = flatten_state(state, total_indices)
    with qml.queuing.QueuingManager.stop_recording():
        probs = qml.probs(wires=wires_to_sample).process_state(flat_state, state_wires)
        # Keep same interface (e.g. jax) as in the device

    return sample_probs(probs, shots, num_wires, is_state_batched, rng, prng_key)


def sample_probs(probs, shots, num_wires, is_state_batched, rng, prng_key=None):
    """
    Sample from given probabilities, dispatching between JAX and NumPy implementations.

    Args:
        probs (array): The probabilities to sample from
        shots (int): The number of samples to take
        num_wires (int): The number of wires to sample
        is_state_batched (bool): whether the state is batched or not
        rng (Union[None, int, array_like[int], SeedSequence, BitGenerator, Generator]):
            A seed-like parameter matching that of ``seed`` for ``numpy.random.default_rng``.
            If no value is provided, a default RNG will be used
        prng_key (Optional[jax.random.PRNGKey]): An optional ``jax.random.PRNGKey``. This is
            the key to the JAX pseudo random number generator. Only for simulation using JAX.
    """
    if qml.math.get_interface(probs) == "jax" or prng_key is not None:
        return _sample_probs_jax(probs, shots, num_wires, is_state_batched, prng_key, seed=rng)

    return _sample_probs_numpy(probs, shots, num_wires, is_state_batched, rng)


def _sample_probs_numpy(probs, shots, num_wires, is_state_batched, rng):
    """
    Sample from given probabilities using NumPy's random number generator.

    Args:
        probs (array): The probabilities to sample from
        shots (int): The number of samples to take
        num_wires (int): The number of wires to sample
        is_state_batched (bool): whether the state is batched or not
        rng (Union[None, int, array_like[int], SeedSequence, BitGenerator, Generator]):
            A seed-like parameter matching that of ``seed`` for ``numpy.random.default_rng``.
            If no value is provided, a default RNG will be used
    """
    rng = np.random.default_rng(rng)
    norm = qml.math.sum(probs, axis=-1)
    norm_err = qml.math.abs(norm - 1.0)
    cutoff = 1e-07

    norm_err = norm_err[..., np.newaxis] if not is_state_batched else norm_err
    if qml.math.any(norm_err > cutoff):
        raise ValueError("probabilities do not sum to 1")

    basis_states = np.arange(2**num_wires)
    if is_state_batched:
        probs = probs / norm[:, np.newaxis] if norm.shape else probs / norm
        samples = np.stack([rng.choice(basis_states, shots, p=p) for p in probs])
    else:
        probs = probs / norm
        samples = rng.choice(basis_states, shots, p=probs)

    powers_of_two = 1 << np.arange(num_wires, dtype=np.int64)[::-1]
    states_sampled_base_ten = samples[..., None] & powers_of_two
    return (states_sampled_base_ten > 0).astype(np.int64)


def _sample_probs_jax(probs, shots, num_wires, is_state_batched, prng_key=None, seed=None):
    """
    Returns a series of samples of a state for the JAX interface based on the PRNG.

    Args:
        probs (array): The probabilities to sample from
        shots (int): The number of samples to take
        num_wires (int): The number of wires to sample
        is_state_batched (bool): whether the state is batched or not
        prng_key (Optional[jax.random.PRNGKey]): An optional ``jax.random.PRNGKey``. This is
            the key to the JAX pseudo random number generator. Only for simulation using JAX.
        seed (Optional[int]): A seed for the random number generator. This is only used if ``prng_key``
            is not provided.

    Returns:
        ndarray[int]: Sample values of the shape (shots, num_wires)
    """
    # pylint: disable=import-outside-toplevel
    import jax
    import jax.numpy as jnp

    if prng_key is None:
        prng_key = jax.random.PRNGKey(np.random.default_rng(seed).integers(100000))

    basis_states = jnp.arange(2**num_wires)

    if is_state_batched:
        keys = jax_random_split(prng_key, num=probs.shape[0])
        samples = jnp.array(
            [
                jax.random.choice(_key, basis_states, shape=(shots,), p=prob)
                for _key, prob in zip(keys, probs)
            ]
        )
    else:
        _, key = jax_random_split(prng_key)
        samples = jax.random.choice(key, basis_states, shape=(shots,), p=probs)

    powers_of_two = 1 << jnp.arange(num_wires, dtype=int)[::-1]
    states_sampled_base_ten = samples[..., None] & powers_of_two
    return (states_sampled_base_ten > 0).astype(int)
