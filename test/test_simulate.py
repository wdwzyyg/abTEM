import hypothesis.strategies as st
import numpy as np
import pytest
from hypothesis import given, reproduce_failure

from abtem import PixelatedDetector, AnnularDetector
from abtem.waves.core import Probe, PlaneWave
from abtem.waves.scan import CustomScan, LineScan, GridScan
from abtem.potentials.potentials import Potential

from utils import gpu
import strategies as abtem_st


# def expected_shape(measurement, waves, potential=None, scan=None, detector=None):
#     shape = ()
#     if potential is not None:
#         if potential.frozen_phonons.ensemble_mean and not np.iscomplexobj(measurement.array):
#             shape += potential.ensemble_shape[1:]
#         else:
#             shape += potential.ensemble_shape
#
#     shape += waves.transforms.ensemble_shape
#
#     if scan is not None:
#         shape += scan.ensemble_shape
#
#     shape = tuple(n for n in shape if n > 1)
#
#     if detector is not None:
#         shape += detector.measurement_shape(waves)
#
#     return shape


@given(data=st.data())
@pytest.mark.parametrize('lazy', [True, False], ids=['lazy', 'not_lazy'])
@pytest.mark.parametrize('device', ['cpu', gpu])
@pytest.mark.parametrize('ensemble_mean', [True, False])
@pytest.mark.parametrize("waves_builder", [
    abtem_st.probe,
    abtem_st.plane_wave,
    abtem_st.s_matrix,
])
def test_multislice_with_frozen_phonons(data, waves_builder, device, ensemble_mean, lazy):
    waves = data.draw(waves_builder(device=device))
    frozen_phonons = data.draw(abtem_st.frozen_phonons())
    exit_waves = waves.multislice(frozen_phonons)

    assert exit_waves.shape[0] == len(frozen_phonons)
    assert len(exit_waves.shape) == len(waves.shape) + 1


@given(data=st.data())
@pytest.mark.parametrize('lazy', [True, False], ids=['lazy', 'not_lazy'])
@pytest.mark.parametrize('device', ['cpu', gpu])
@pytest.mark.parametrize('ensemble_mean', [True, False])
@pytest.mark.parametrize("waves_builder", [
    abtem_st.probe,
    abtem_st.plane_wave,
])
def test_multislice_detect_with_frozen_phonons(data, waves_builder, device, ensemble_mean, lazy):
    waves = data.draw(waves_builder(device=device, allow_distribution=False))
    frozen_phonons = data.draw(abtem_st.frozen_phonons(ensemble_mean=ensemble_mean))

    detector = PixelatedDetector(max_angle=None)
    exit_waves = waves.multislice(frozen_phonons, detectors=detector, lazy=lazy)

    if ensemble_mean:
        assert exit_waves.shape == waves.shape
        assert len(exit_waves.shape) == len(waves.shape)
    else:
        assert exit_waves.shape[0] == len(frozen_phonons)
        assert len(exit_waves.shape) == len(waves.shape) + 1


@given(data=st.data())
@pytest.mark.parametrize('lazy', [True, False], ids=['lazy', 'not_lazy'])
@pytest.mark.parametrize('device', ['cpu', gpu])
@pytest.mark.parametrize('ensemble_mean', [True, False])
@pytest.mark.parametrize("waves_builder", [
    abtem_st.s_matrix,
])
def test_s_matrix_multislice_detect_with_frozen_phonons(data, waves_builder, device, ensemble_mean, lazy):
    waves = data.draw(waves_builder(device=device, allow_distribution=False))
    frozen_phonons = data.draw(abtem_st.frozen_phonons(ensemble_mean=ensemble_mean))

    detector = PixelatedDetector(max_angle=None)

    exit_waves = waves.multislice(frozen_phonons).reduce(detectors=detector)

    if ensemble_mean:
        assert len(exit_waves.shape) == len(waves.shape) - 1
    else:
        assert exit_waves.shape[0] == len(frozen_phonons)
        assert len(exit_waves.shape) == len(waves.shape)


@given(data=st.data())
@pytest.mark.parametrize('lazy', [True, False], ids=['lazy', 'not_lazy'])
@pytest.mark.parametrize('device', ['cpu', gpu])
@pytest.mark.parametrize("waves_builder", [
    abtem_st.probe,
    abtem_st.plane_wave,
])
def test_multislice_thickness_series(data, waves_builder, device, lazy):
    waves = data.draw(waves_builder(device=device, allow_distribution=False))
    potential = data.draw(abtem_st.potential(exit_planes=True, ensemble_mean=False))
    exit_waves = waves.multislice(potential, lazy=lazy)

    assert exit_waves.shape[1] == len(potential.exit_planes)
    assert exit_waves.shape[0] == potential.num_frozen_phonons
    assert exit_waves.gpts == potential.gpts


@given(data=st.data())
@pytest.mark.parametrize('lazy', [True, False], ids=['lazy', 'not_lazy'])
@pytest.mark.parametrize('device', ['cpu', gpu])
@pytest.mark.parametrize('frozen_phonons', [True, False])
@pytest.mark.parametrize('detector', [
    abtem_st.segmented_detector,
    abtem_st.flexible_annular_detector,
    abtem_st.pixelated_detector,
    abtem_st.waves_detector,
    abtem_st.annular_detector
])
@pytest.mark.parametrize('scan', [
    GridScan(),
    LineScan(),
    CustomScan(),
])
@pytest.mark.parametrize("waves_builder", [
    abtem_st.probe,
])
def test_probe_scan(data, waves_builder, detector, scan, device, frozen_phonons, lazy):
    probe = data.draw(waves_builder(allow_distribution=False))
    detector = data.draw(detector())

    potential = data.draw(abtem_st.potential(no_frozen_phonons=not frozen_phonons, ensemble_mean=False))
    scan.match_probe(probe)

    if isinstance(scan, CustomScan) and isinstance(detector, AnnularDetector):
        return

    measurement = probe.scan(potential, scan=scan, detectors=detector, lazy=lazy)

    measurement_shape = detector.measurement_shape(probe)
    assert measurement.shape == potential.ensemble_shape + scan.ensemble_shape + measurement_shape
    assert measurement.dtype == detector.measurement_dtype
    assert type(measurement) == detector.measurement_type(probe.build(scan))

    if not isinstance(detector, AnnularDetector):
        assert measurement.base_axes_metadata == detector.measurement_axes_metadata(probe.build(scan))
#
# # @given(data=st.data(),
# #        gpts=core_st.gpts(min_value=32, max_value=64),
# #        planewave_cutoff=st.floats(5, 10),
# #        energy=st.floats(100e3, 200e3))
# # @pytest.mark.parametrize('lazy', [True, False])
# # @pytest.mark.parametrize('device', ['cpu', gpu])
# # @pytest.mark.parametrize('detector', list(all_detectors.keys()))
# # @pytest.mark.parametrize('downsample', ['cutoff', False])
# # @pytest.mark.parametrize('interpolation', [1, 2, 3])
# # @pytest.mark.parametrize('frozen_phonons', [True, False])
# # def test_s_matrix_scan_and_detect(data,
# #                                   gpts,
# #                                   planewave_cutoff,
# #                                   energy,
# #                                   detector,
# #                                   lazy,
# #                                   device,
# #                                   downsample,
# #                                   interpolation,
# #                                   frozen_phonons):
# #     if frozen_phonons:
# #         atoms = data.draw(atoms_st.random_atoms(min_side_length=5, max_side_length=10))
# #     else:
# #         atoms = data.draw(atoms_st.random_frozen_phonons(min_side_length=5, max_side_length=10))
# #
# #     s_matrix = SMatrix(potential=atoms,
# #                        gpts=gpts,
# #                        planewave_cutoff=planewave_cutoff,
# #                        interpolation=interpolation,
# #                        energy=energy,
# #                        device=device)
# #
# #     if downsample:
# #         probe = s_matrix.build(stop=0, lazy=True).downsample(max_angle=downsample).comparable_probe()
# #     else:
# #         probe = s_matrix.build(stop=0, lazy=True).comparable_probe()
# #
# #     detectors = [
# #         data.draw(all_detectors[detector](max_angle=np.floor(min(probe.cutoff_angles)), allow_detect_every=False))]
# #
# #     scan = GridScan()
# #     measurements = s_matrix.scan(scan=scan, detectors=detectors, lazy=lazy, downsample=downsample)
# #     measurements.compute()
# #
# #     assert_scanned_measurement_as_expected(measurements, atoms, probe, detectors, scan=scan)
