# Selected-phone lag-test bench observation

This directory records the illustrative setup check reported in the
CaptureBridge SoftwareX manuscript. It is not a formal phone-to-phone
synchronization validation dataset.

## Reported setup

- phone: Samsung Galaxy S25 Ultra;
- visual target: CaptureBridge Hub fullscreen ArUco timing display;
- display refresh rate: 144 Hz (6.94 ms per refresh);
- capture modes: 240 fps and 60 fps; and
- evaluated boundaries: the first and last saved frames, corresponding to the
  visual start and stop targets.

The authors observed endpoint deviations of 0--2 captured frames at 240 fps and
0--1 captured frame at 60 fps for both boundaries. Those ranges correspond to
0--8.33 ms and 0--16.67 ms, respectively. The conversion is based on the
nominal capture periods, 1000/240 ms and 1000/60 ms.

The values are preserved in
[lag_bench_observation.csv](lag_bench_observation.csv). They are an
author-reported bench observation, not summary statistics from the older local
development archive. Resolution, transport, and repetition count were not
preserved with this concise observation. Accordingly, the manuscript does not
claim traceable timing metrology, a population estimate, or frame-level
phone-to-phone synchronization.

## Reproduction protocol

1. Use the tagged Hub and Android releases and set camera lead compensation to
   zero.
2. Connect one phone through authorized USB/ADB reverse or a trusted private
   WLAN and select the camera profile to be tested.
3. Aim the phone at the fullscreen lag-test target so the complete green border
   and both timing markers remain visible.
4. Run the selected-phone Lag Test. The Hub pre-arms the encoder, refreshes
   clock samples, uses scheduled phone-clock boundaries when synchronization is
   usable, transfers the result, and analyzes the first and last saved frames.
5. Retain the generated MP4, state/segment/camera-time sidecars, JSON/CSV lag
   reports, and endpoint images.
6. For a formal validation, repeat a predeclared number of trials for every
   phone, mode, transport, and load condition; publish every inclusion and
   exclusion, signed endpoint differences, and summary statistics.

The visual method is quantized by the phone frame period, display refresh,
display-to-camera phase, rolling shutter, and the Hub display scheduler. It
tests one phone against a visual target; it does not replace simultaneous
optical or electronic phone-to-phone/reference-system validation.
