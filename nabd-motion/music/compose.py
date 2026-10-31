"""An original, royalty-free music bed for the Nabd video, synthesized from scratch (no samples).

A soft pad, a plucked arpeggio, a sub bass and a heartbeat (Nabd means "pulse"). The first 68.5 s follow
the scenes of the short promo (calm intro, the store comes alive, tense problem, then the progression opens
up through Mizan, the learned laws, safety and the benchmark, and resolves on the logo). After that a calmer
loop runs under the live demo until about 3:03.

    python music/compose.py      # writes out/nabd-music-3min.wav and out/nabd-music-promo.wav
"""

import os
import wave

import numpy as np
from scipy.signal import fftconvolve

SR = 44100
TOTAL = 183.0
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "out")
rng = np.random.default_rng(7)

L = np.zeros(int(TOTAL * SR))
R = np.zeros(int(TOTAL * SR))


def hz(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


def place(sig_l, sig_r, t0):
    i = int(t0 * SR)
    n = min(len(sig_l), len(L) - i)
    if n > 0:
        L[i:i + n] += sig_l[:n]
        R[i:i + n] += sig_r[:n]


def env(n, attack, release, sustain_level=1.0):
    t = np.arange(n) / SR
    a = np.clip(t / max(attack, 1e-3), 0, 1)
    r = np.clip((n / SR - t) / max(release, 1e-3), 0, 1)
    return np.minimum(a, r) * sustain_level


def pad_note(m, dur, amp, attack=1.2, release=1.6, bright=1.0):
    """Warm additive saw, three detuned voices per channel."""
    n = int((dur + release) * SR)
    t = np.arange(n) / SR
    f = hz(m)
    out = []
    for side, detunes in ((0, (-7, 0, 5)), (1, (-4, 2, 8))):
        s = np.zeros(n)
        for c in detunes:
            fc = f * 2 ** (c / 1200)
            for k in range(1, 9):
                if fc * k > 5000:
                    break
                s += np.sin(2 * np.pi * fc * k * t + rng.uniform(0, 6.28)) / k ** (1.45 - 0.3 * bright)
        out.append(s)
    e = env(n, attack, release) * amp / 6
    return out[0] * e, out[1] * e


def pluck(m, amp, pan, decay=0.45):
    n = int(1.2 * SR)
    t = np.arange(n) / SR
    f = hz(m)
    s = np.sin(2 * np.pi * f * t) + 0.35 * np.sin(4 * np.pi * f * t) + 0.12 * np.sin(6 * np.pi * f * t)
    e = np.exp(-t / decay) * np.clip(t / 0.004, 0, 1) * amp * 2.2
    return s * e * (1 - pan), s * e * (1 + pan)


def bass(m, dur, amp):
    n = int((dur + 0.6) * SR)
    t = np.arange(n) / SR
    f = hz(m)
    s = np.sin(2 * np.pi * f * t) + 0.6 * np.sin(4 * np.pi * f * t) + 0.2 * np.sin(6 * np.pi * f * t)
    e = env(n, 0.25, 0.6) * amp * 0.3  # felt more than heard: laptop speakers drop the fundamental anyway
    return s * e, s * e


def beat(amp):
    """One heartbeat: lub, then a softer dub."""
    n = int(0.6 * SR)
    t = np.arange(n) / SR
    out = np.zeros(n)
    for start, a in ((0.0, 1.0), (0.21, 0.7)):
        tt = t - start
        m = tt >= 0
        f = 105 - 50 * np.clip(tt[m] / 0.1, 0, 1)
        ph = 2 * np.pi * np.cumsum(f) / SR
        out[m] += a * np.sin(ph) * np.exp(-tt[m] / 0.09)
        out[m] += a * 0.35 * np.sin(2 * np.pi * 190 * tt[m]) * np.exp(-tt[m] / 0.018)  # the audible knock
    return out * amp, out * amp


def tick(amp, pan):
    n = int(0.06 * SR)
    t = np.arange(n) / SR
    s = rng.normal(0, 1, n)
    s = np.diff(s, prepend=0)  # brighten
    e = np.exp(-t / 0.012) * amp
    return s * e * (1 - pan), s * e * (1 + pan)


CH = {  # chord tones (pad), bass note, arpeggio tones
    "Am": ([57, 60, 64], 45, [69, 72, 76, 81]),
    "F": ([53, 57, 60], 41, [65, 69, 72, 77]),
    "C": ([55, 60, 64], 36, [67, 72, 76, 79]),
    "G": ([55, 59, 62], 43, [67, 71, 74, 79]),
    "E": ([56, 59, 64], 40, [68, 71, 76, 80]),
    "Cadd9": ([55, 60, 64, 74], 36, [67, 72, 74, 79]),
}


def chords(t0, names, each, pad_amp, bass_amp, arp=None, bright=1.0, octave_up=False):
    """Lay a chord progression from t0; arp = (step seconds, amplitude) or None."""
    for i, name in enumerate(names):
        tones, b, arp_tones = CH[name]
        ts = t0 + i * each
        for m in tones:
            l, r = pad_note(m, each, pad_amp, bright=bright)
            place(l, r, ts)
            if octave_up:
                l, r = pad_note(m + 12, each, pad_amp * 0.45, bright=bright)
                place(l, r, ts)
        l, r = bass(b, each, bass_amp)
        place(l, r, ts)
        if arp:
            step, amp = arp
            k = 0
            seq = arp_tones + arp_tones[-2:0:-1]
            tt = ts
            while tt < ts + each - 1e-6:
                l, r = pluck(seq[k % len(seq)], amp, pan=0.35 * (1 if k % 2 else -1))
                place(l, r, tt)
                k += 1
                tt += step


def heartbeats(t0, t1, period, amp):
    t = t0
    while t < t1:
        l, r = beat(amp)
        place(l, r, t)
        t += period


# Scene lengths in frames, as in NabdPromoShort (nabd-motion/src/Video.tsx): intro, Dukkan (cut before its
# storm), problem, Mizan, learned laws, safety, benchmark, outro. Each transition overlaps 18 frames.
SCENES, FPS, TR = [180, 170, 250, 370, 390, 310, 290, 220], 30, 18
S = [sum(SCENES[:i]) / FPS - i * TR / FPS for i in range(len(SCENES))]
PROMO = (sum(SCENES) - TR * (len(SCENES) - 1)) / FPS
S.append(PROMO)

# intro: one chord breathes in, the pulse starts
chords(0.0, ["Am"], 5.6, 0.55, 0.0)
heartbeats(1.4, 5.4, 0.9, 0.55)
# Dukkan: the store comes alive, warm and moving, the pulse goes on
chords(S[1], ["C", "G"], (S[2] - S[1]) / 2, 0.55, 0.35, arp=(0.25, 0.08))
heartbeats(S[1] + 0.1, S[2] - 0.4, 0.9, 0.4)
# problem: tension, F then E, the pulse a little faster
chords(S[2], ["F", "E"], (S[3] - S[2]) / 2, 0.6, 0.35)
heartbeats(S[2] + 0.2, S[3] - 0.4, 0.8, 0.45)
# mizan: the progression opens, the arpeggio enters
chords(S[3], ["Am", "F", "C", "G"], (S[4] - S[3]) / 4, 0.55, 0.4, arp=(0.25, 0.10))
# learned laws: fuller, a high shimmer on each chord
chords(S[4], ["Am", "F", "C", "G"], (S[5] - S[4]) / 4, 0.55, 0.42, arp=(0.25, 0.13), bright=1.2)
for i in range(4):
    l, r = pluck(88 if i % 2 == 0 else 84, 0.05, pan=0.6 * (1 if i % 2 else -1), decay=1.6)
    place(l, r, S[4] + i * (S[5] - S[4]) / 4)
# safety: steady
chords(S[5], ["F", "C", "G", "Am"], (S[6] - S[5]) / 4, 0.55, 0.42, arp=(0.25, 0.12))
# benchmark: the lift, brighter, soft ticks
chords(S[6], ["C", "G", "Am", "F"], (S[7] - S[6]) / 4, 0.6, 0.45, arp=(0.25, 0.14), bright=1.4, octave_up=True)
t = S[6]
while t < S[7] - 0.1:
    l, r = tick(0.05, pan=0.2)
    place(l, r, t + 0.125)
    t += 0.25
# outro: resolve on the logo, two last heartbeats, fade
chords(S[7], ["Cadd9"], PROMO - S[7] + 1.0, 0.6, 0.4, bright=1.1)
heartbeats(S[7] + 0.45, S[7] + 2.3, 0.9, 0.6)
for i, m in enumerate([84, 88, 91]):
    l, r = pluck(m, 0.05, pan=0.4 * (i - 1), decay=2.0)
    place(l, r, S[7] + 1.2 + i * 0.4)

# under the live demo: calm loop, sparse arpeggio, a soft pulse every other bar
t = PROMO + 0.6
names = ["Am", "F", "C", "G"]
i = 0
while t < TOTAL - 4:
    name = names[i % 4]
    chords(t, [name], 3.2, 0.42, 0.3, arp=(0.5, 0.07))
    if i % 2 == 0:
        heartbeats(t + 0.1, t + 1.0, 0.9, 0.28)
    t += 3.2
    i += 1

# room: a short synthetic reverb
ir_n = int(2.4 * SR)
ir_t = np.arange(ir_n) / SR
ir_l = rng.normal(0, 1, ir_n) * np.exp(-ir_t / 0.55)
ir_r = rng.normal(0, 1, ir_n) * np.exp(-ir_t / 0.55)
ir_l /= np.sqrt((ir_l ** 2).sum())
ir_r /= np.sqrt((ir_r ** 2).sum())
wet_l = fftconvolve(L, ir_l)[:len(L)]
wet_r = fftconvolve(R, ir_r)[:len(R)]
mix_l = 0.78 * L + 0.32 * wet_l
mix_r = 0.78 * R + 0.32 * wet_r

# fades, soft limiting, normalization to -1 dBFS
n = len(mix_l)
fade = np.ones(n)
end = int((TOTAL - 0.2) * SR)
fade[end - int(5 * SR):end] = np.linspace(1, 0, int(5 * SR))
fade[end:] = 0
mix = np.stack([mix_l * fade, mix_r * fade], axis=1)
mix = np.tanh(mix / (np.abs(mix).max() * 0.9) * 1.2)
mix /= np.abs(mix).max() / 0.89


def write(path, data):
    pcm = (np.clip(data, -1, 1) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


os.makedirs(OUT, exist_ok=True)
write(os.path.join(OUT, "nabd-music-3min.wav"), mix)
promo = mix[:int(PROMO * SR)].copy()
k = int(2.2 * SR)
promo[-k:] *= np.linspace(1, 0, k)[:, None]
write(os.path.join(OUT, "nabd-music-promo.wav"), promo)
print("wrote", os.path.join(OUT, "nabd-music-3min.wav"), "and nabd-music-promo.wav")
