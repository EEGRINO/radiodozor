from pathlib import Path
import tempfile
import unittest

import numpy as np

from csi import CARRIERS, Frame, HEADER, from_udp, from_visor
from sensing import CALIBRATION, COUNTDOWN, HZ, WINDOW, Channel, pulse_candidate, rhythm
from server import Station


def make_frame(sequence, swing=0, receiver="192.0.2.10"):
    samples = np.zeros((64, 2), dtype=np.int8)
    samples[CARRIERS, 0] = 31
    samples[CARRIERS[:26], 1] = 61 + swing
    samples[CARRIERS[26:], 1] = 61 - swing
    return Frame(receiver, "A1B2", 6, False, sequence, -48, samples.tobytes())


class SensingTests(unittest.TestCase):
    def test_udp_and_existing_visor_agree(self):
        original = make_frame(32, 3)
        wire = HEADER.pack(b"RUV2", 32, 1000, -48, -95, 6, 0, 64, 0xA1B2) + original.iq
        self.assertEqual(from_udp(wire, original.receiver), original)
        self.assertEqual(from_visor(original.wire_view(.03)), original)
        self.assertIsNone(from_udp(wire[:-2], original.receiver))
        self.assertIsNone(from_visor(original.wire_view(.8)))
        self.assertAlmostEqual(float(original.profile().mean()), 1, places=6)

    def test_freq_estimation_and_no_invented_rate_in_empty_stream(self):
        t = np.arange(WINDOW * HZ) / HZ
        wave = .009 * np.sin(2 * np.pi * .26 * t)
        x = np.stack((1 + wave, 1 - wave), axis=1)
        bpm, power, concentration, plot = rhythm(x)
        self.assertAlmostEqual(bpm, 15.6, delta=.3)
        self.assertGreater(power, .006)
        self.assertGreater(concentration, .9)
        self.assertEqual(len(plot), 100)
        self.assertIsNone(rhythm(np.ones_like(x))[0])

    def test_baseline_rhythm_alarm_motion_suppression_and_signal_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            station = Station(data_dir)
            start = 100.
            sequence = 1
            station.ingest(make_frame(sequence), start)
            key = make_frame(0).key
            self.assertIsNotNone(station.action("arm", start))
            self.assertIsNone(station.action("calibrate", start))
            end = start + COUNTDOWN + CALIBRATION
            for n in range(1, (COUNTDOWN + CALIBRATION) * HZ + 1):
                sequence += 1
                station.ingest(make_frame(sequence), start + n / HZ + .015)
            station.update(end + .05)
            self.assertEqual(station.snapshot(end + .05)["calibration_result"], "ready")
            self.assertIn(key, Station(data_dir).baselines)
            self.assertEqual(station.results[key]["state"], "quiet")
            self.assertIsNone(station.action("arm", end + .05))

            for n in range(1, 45 * HZ + 1):
                sequence += 1
                swing = round(np.sin(2 * np.pi * .25 * n / HZ))
                at = end + n / HZ + .015
                station.ingest(make_frame(sequence, swing), at)
                if n % HZ == 0:
                    station.update(at)
            self.assertEqual(station.results[key]["state"], "rhythm")
            self.assertAlmostEqual(station.results[key]["rpm"], 15., delta=.4)
            self.assertEqual(station.alarm, "rhythm")

            self.assertIsNone(station.action("clear", at))
            for n in range(1, 31):
                sequence += 1
                station.ingest(make_frame(sequence, (-1)**n * 10), at + n / HZ)
                if n % HZ == 0:
                    station.update(at + n / HZ)
            self.assertEqual(station.results[key]["state"], "motion")
            self.assertNotIn("rpm", station.results[key])
            self.assertEqual(station.alarm, "motion")
            station.update(at + 10)
            self.assertEqual(station.results[key]["state"], "offline")
            self.assertEqual(station.snapshot(at + 10)["lines"], [])
            self.assertIn(b'"reason": "motion"', station.journal())

    def test_sparse_window_and_sequence_restart(self):
        line = Channel()
        for n in range(50):
            line.add(make_frame(n + 1000), 80 + n / HZ + .02)
        self.assertIsNotNone(line.window(85, 15))
        line.add(make_frame(0), 85.1)
        self.assertIsNone(line.window(85.2, 15))
        self.assertIsNone(line.calibrate(85.2))

    def test_pulse_candidate_requires_a_stable_separate_peak(self):
        t = np.arange(WINDOW * HZ) / HZ
        breathing = .01 * np.sin(2 * np.pi * .25 * t)
        pulse = .003 * np.sin(2 * np.pi * 1.12 * t)
        samples = lambda x: np.stack((1 + breathing + x, 1 - breathing - x), axis=1)
        self.assertAlmostEqual(pulse_candidate(samples(pulse), 15, .001), 67, delta=1)
        self.assertIsNone(pulse_candidate(samples(pulse), 15, None))
        self.assertIsNone(pulse_candidate(samples(pulse), 15, .004))
        self.assertIsNone(pulse_candidate(samples(.003 * np.sin(2 * np.pi * 1.25 * t)), 15, .001))
        pulse[len(t) // 2:] = 0
        self.assertIsNone(pulse_candidate(samples(pulse), 15, .001))

    def test_sleep_uses_a_separate_background_and_disables_room_alarm(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            frame = make_frame(0)
            (directory / "baseline.json").write_text(
                '{"' + frame.key + '":{"motion":0.02,"rhythm":0.003}}', encoding="utf-8",
            )
            station = Station(directory)
            start = 101.
            station.ingest(frame, start)
            self.assertIsNone(station.baselines[frame.key].pulse)
            self.assertIsNone(station.action("arm", start))
            self.assertIsNotNone(station.action("sleep", start))
            self.assertIsNone(station.action("disarm", start))
            self.assertIsNone(station.action("sleep", start))
            self.assertFalse(station.snapshot(start)["lines"][0]["calibrated"])
            self.assertIsNotNone(station.action("arm", start))
            self.assertIsNone(station.action("calibrate", start))
            self.assertIsNotNone(station.action("guard", start))
            end = start + COUNTDOWN + CALIBRATION
            for i in range(1, (COUNTDOWN + CALIBRATION) * HZ + 1):
                station.ingest(make_frame(i), start + i / HZ + .015)
            station.update(end + .05)
            key = frame.key
            self.assertEqual(station.results[key]["state"], "settling")
            self.assertTrue((directory / "sleep_baseline.json").exists())
            self.assertEqual(Station(directory).baselines[key].motion, .02)

            for i in range(1, 45 * HZ + 1):
                swing = round(np.sin(2 * np.pi * .25 * i / HZ))
                at = end + i / HZ + .015
                station.ingest(make_frame(i + (COUNTDOWN + CALIBRATION) * HZ, swing), at)
                if i % HZ == 0:
                    station.update(at)
            result = station.results[key]
            self.assertEqual(result["state"], "rhythm")
            self.assertGreater(result["breath_strength"], 1)
            self.assertIsNone(result["pulse_candidate"])
            self.assertIsNone(station.alarm)
            self.assertTrue(station.snapshot(at)["lines"][0]["calibrated"])
            station.update(at + 3)
            self.assertEqual(station.snapshot(at + 3)["lines"], [])
