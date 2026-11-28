# Synthetic container fixtures

These small offline fixtures test NWAS v2 container structure and clip
coverage. The 0.2-second pair tests malformed containers and is too short for
the film's 3.1-second cue. The four-second pair can cover the synthetic launch
reference and reported cue times. None of them contains a measured film
cue or qualifies as physical evidence.

```sh
ffmpeg -f lavfi -i 'testsrc2=size=16x16:rate=10:duration=0.2' \
  -f lavfi -i 'sine=frequency=1760:sample_rate=48000:duration=0.2' \
  -c:v mpeg4 -c:a aac -shortest synthetic-mux.mp4
ffmpeg -i synthetic-mux.mp4 -map 0:v -c copy synthetic-video.mp4

ffmpeg -f lavfi -i 'testsrc2=size=16x16:rate=10:duration=4' \
  -f lavfi -i 'sine=frequency=1760:sample_rate=48000:duration=4' \
  -c:v mpeg4 -q:v 7 -c:a aac -b:a 32k -shortest synthetic-timeline-mux.mp4
ffmpeg -i synthetic-timeline-mux.mp4 -map 0:v -c copy synthetic-timeline-video.mp4

for rate in 88200 96000; do
  ffmpeg -f lavfi -i 'color=c=black:size=16x16:rate=25:d=4' \
    -f lavfi -i "anullsrc=r=$rate:cl=mono:d=4" -c:v libx264 -threads 1 -preset ultrafast \
    -c:a aac -b:a 24k -shortest -fflags +bitexact -flags:v +bitexact -flags:a +bitexact \
    "synthetic-aac-$rate.mp4"
done
ffmpeg -f lavfi -i 'color=c=black:size=16x16:rate=25:d=4' \
  -f lavfi -i 'anullsrc=r=96000:cl=mono:d=4' -c:v libx264 -threads 1 -preset ultrafast \
  -c:a alac -shortest -fflags +bitexact -flags:v +bitexact -flags:a +bitexact synthetic-alac-96000.mov

for rate in 96000 192000; do
  ffmpeg -f lavfi -i 'color=c=black:size=16x16:rate=25:d=4' \
    -f lavfi -i "anullsrc=r=$rate:cl=stereo:d=4" -c:v libx264 -threads 1 -preset ultrafast \
    -c:a alac -sample_fmt s16p -shortest -fflags +bitexact -flags:v +bitexact -flags:a +bitexact \
    "synthetic-alac-$rate.mp4"
done
```

The Python tests use the checked-in bytes and do not require FFmpeg.
The high-rate AAC/ALAC files use silence and black frames to keep the
fixtures small. They exercise real encoder/muxer output, not measured cues.
