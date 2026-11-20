// For each half-second keep just a typical voiced pitch, never a voice recording.
// Local diarization labels can group two people on the same speakerphone.
function voicedPitch(frame) {
  const signal = new Float32Array(160);
  let mean = 0;
  for (let i = 0; i < signal.length; i++) {
    const j = i * 4;
    signal[i] = (frame[j] + frame[j + 1] + frame[j + 2] + frame[j + 3]) / 4;
    mean += signal[i];
  }
  mean /= signal.length;
  let power = 0;
  for (let i = 0; i < signal.length; i++) {
    signal[i] -= mean;
    power += signal[i] ** 2;
  }
  if (power / signal.length < 0.000001) return 0;

  const correlation = new Float32Array(56);
  for (let lag = 10; lag < correlation.length; lag++) {
    let cross = 0, left = 0, right = 0;
    for (let i = 0; i < 104; i++) {
      cross += signal[i] * signal[i + lag];
      left += signal[i] ** 2;
      right += signal[i + lag] ** 2;
    }
    correlation[lag] = cross / Math.sqrt(left * right || 1);
  }
  const threshold = Math.max(0.71, 0.91 * Math.max(...correlation.subarray(11, 55)));
  for (let lag = 11; lag < 55; lag++) {
    if (correlation[lag] >= threshold && correlation[lag] >= correlation[lag - 1]
        && correlation[lag] >= correlation[lag + 1]) return Math.round(2000 / lag);
  }
  return 0;
}

export function voiceCues(seconds) {
  // Web worker receives 16 kHz mono PCM after the existing remix and resample.
  const buckets = Array.from({ length: Math.ceil(seconds * 2) }, () => []);
  const frame = new Float32Array(640);
  let cursor = 0;
  function add(samples, startSample = cursor) {
    for (let i = 0; i < samples.length; i++) {
      const position = startSample + i;
      const within = position % 2000;
      if (within >= 640 || position < 0) continue;
      frame[within] = samples[i];
      if (within === 639) {
        const value = voicedPitch(frame);
        if (value) buckets[Math.floor(position / 8000)]?.push(value);
      }
    }
    cursor = startSample + samples.length;
  }
  function bytes() {
    return Uint8Array.from(buckets, values => {
      if (!values.length) return 0;
      values.sort((a, b) => a - b);
      return values[Math.floor(values.length / 2)];
    });
  }
  return { add, bytes };
}
