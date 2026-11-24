"""Estimate changes in a calibrated radio link and look for a sustained low-frequency rhythm."""

from collections import deque
from dataclasses import dataclass

import numpy as np

HZ = 10
WINDOW = 40
COUNTDOWN = 12
CALIBRATION = 45
MOTION_SAMPLES = 15
STALE = 2
PULSE_BAND = (.8, 2.15)


@dataclass(frozen=True)
class Baseline:
    motion: float
    rhythm: float
    pulse: float | None = None


class Channel:
    def __init__(self):
        self.frame = None
        self.last = 0.0
        self.last_motion = -float("inf")
        self.bins = deque(maxlen=(WINDOW + 2) * HZ)

    def add(self, frame, now):
        if self.frame is not None:
            step = (frame.sequence - self.frame.sequence) & 0xffffffff
            gap = now - self.last
            if gap <= STALE and (step == 0 or step > 0xffffffff - 50):
                return
            if step > 0x80000000 or gap > STALE:
                self.bins.clear()
                self.last_motion = now
        self.frame, self.last = frame, now
        profile = frame.profile()
        if profile is None:
            return
        tick = int(now * HZ)
        if self.bins and tick == self.bins[-1][0]:
            _, average, n = self.bins[-1]
            self.bins[-1] = (tick, average + (profile - average) / (n + 1), n + 1)
        else:
            self.bins.append((tick, profile, 1))

    def window(self, now, count):
        end = int(now * HZ) - 1
        start = end - count + 1
        data = [(tick, average) for tick, average, _ in self.bins if start <= tick <= end]
        if len(data) < .85 * count:
            return None
        ticks = np.asarray([tick for tick, _ in data])
        if np.max(np.diff(np.r_[start, ticks, end])) > 5:
            return None
        values = np.asarray([value for _, value in data])
        if len(data) == count:
            return values
        grid = np.arange(start, end + 1)
        return np.column_stack([np.interp(grid, ticks, column) for column in values.T])

    def calibrate(self, end):
        window = self.window(end, WINDOW * HZ)
        if window is None:
            return None
        motion_floor = np.quantile([variation(window[i:i + MOTION_SAMPLES])
                                    for i in range(0, len(window) - MOTION_SAMPLES + 1, 5)], .95)
        return Baseline(
            max(.012, float(motion_floor) * 2.6),
            max(.002, rhythm(window)[1] * 2.5),
            max(.001, _pulse_peak(window)[1] * 4),
        )

    def measure(self, now, baseline, sleep_since=None):
        if now - self.last > STALE:
            return {"state": "offline"}
        if baseline is None:
            return {"state": "needs_calibration"}
        short = self.window(now, MOTION_SAMPLES)
        if short is None:
            return {"state": "weak_stream"}
        score = variation(short) / baseline.motion
        result = {"state": "quiet", "motion": round(score, 2)}
        if score >= 1:
            self.last_motion = now
            return {**result, "state": "motion"}
        quiet_since = self.last_motion if sleep_since is None else max(self.last_motion, sleep_since)
        if now - quiet_since < WINDOW:
            if sleep_since is not None:
                return {"state": "settling"}
            return result
        long = self.window(now, WINDOW * HZ)
        if long is None:
            return result
        rate, power, concentration, wave = rhythm(long)
        result["rhythm_strength"] = round(power, 4)
        if power >= baseline.rhythm and concentration >= .53 and rate is not None:
            result.update(state="rhythm", rpm=round(rate, 1), wave=wave)
            if sleep_since is not None:
                result["breath_strength"] = round(power / baseline.rhythm, 2)
                result["pulse_candidate"] = pulse_candidate(long, rate, baseline.pulse)
        return result


def variation(samples):
    """Median per-carrier standard deviation over a short time window."""
    return float(np.median(np.std(samples, axis=0)))


def _spectrum(samples):
    n = len(samples)
    t = np.linspace(-1, 1, n)
    centered = samples - samples.mean(axis=0)
    detrended = centered - np.outer(t, (t @ centered) / (t @ t))
    taper = np.hanning(n)
    spectra = np.fft.rfft(detrended * taper[:, None], axis=0)
    power = (np.abs(spectra) ** 2).mean(axis=1)
    frequencies = np.fft.rfftfreq(n, 1 / HZ)
    return detrended, frequencies, spectra, power, taper.sum()


def _peak(frequencies, power, taper_sum, band_limits, broad_limits):
    low, high = band_limits
    band = np.flatnonzero((frequencies >= low) & (frequencies <= high))
    broad = (frequencies >= broad_limits[0]) & (frequencies <= broad_limits[1])
    index = int(band[np.argmax(power[band])])
    near = slice(index - 1, index + 2)
    concentration = float(power[near].sum() / max(power[broad].sum(), 1e-15))
    strength = float(2 * np.sqrt(power[index]) / taper_sum)
    if strength < 1e-10:
        return None, 0., 0., index
    a, b, c = np.log(np.maximum(power[near], 1e-15))
    delta = np.clip(.5 * (a - c) / min(a - 2 * b + c, -1e-12), -.5, .5)
    frequency = float(frequencies[index] + delta * frequencies[1])
    return (frequency if low <= frequency <= high else None), strength, concentration, index


def rhythm(samples):
    """Calculate the strongest respiratory-band peak and its share of nearby spectral power."""
    detrended, frequencies, spectra, power, taper_sum = _spectrum(samples)
    peak, strength, concentration, index = _peak(frequencies, power, taper_sum, (.1, .5), (.075, 1.5))
    if peak is None:
        return None, strength, concentration, []
    carrier = int(np.argmax(np.abs(spectra[index])))
    selected = np.fft.rfft(detrended[:, carrier])
    selected[~((frequencies >= .1) & (frequencies <= .5))] = 0
    wave = np.fft.irfft(selected, len(samples))[::4]
    wave /= max(float(np.max(np.abs(wave))), 1e-9)
    return peak * 60, strength, concentration, np.round(wave, 3).tolist()


def _pulse_peak(samples):
    _, frequencies, _, power, taper_sum = _spectrum(samples)
    return _peak(frequencies, power, taper_sum, PULSE_BAND, (.65, 2.45))[:3]


def pulse_candidate(samples, breathing_rpm, noise_limit):
    """Return a separately sustained spectral candidate, excluding breathing harmonics."""
    if noise_limit is None:
        return None
    frequency, strength, fractional_power = _pulse_peak(samples)
    if frequency is None or strength < noise_limit or fractional_power < .45:
        return None
    breathing_hz = breathing_rpm / 60
    harmonic = round(frequency / breathing_hz)
    if abs(frequency - harmonic * breathing_hz) <= .045:
        return None
    for block in np.array_split(samples, 2):
        peak, level, concentration = _pulse_peak(block)
        if peak is None or abs(peak - frequency) > .075 or level < noise_limit or concentration < .30:
            return None
    return round(frequency * 60)
