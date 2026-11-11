// Firefox only lists real media devices in enumerateDevices() after getUserMedia has been called in the
// document. Chrome (with --use-fake-ui-for-media-stream) lists them up front, and Meet shows "Mic not found"
// otherwise, because the payload's getUserMedia interceptor never calls the real getUserMedia.
// So call it once here (permission is auto-granted by pref) and make enumerateDevices wait for it.
(() => {
  const mediaDevices = navigator.mediaDevices;
  if (!mediaDevices) return;
  const originalEnumerateDevices = mediaDevices.enumerateDevices.bind(mediaDevices);
  const primed = mediaDevices
    .getUserMedia({ audio: true, video: true })
    .then((stream) => stream.getTracks().forEach((track) => track.stop()))
    .catch(() => {});
  mediaDevices.enumerateDevices = async function enumerateDevices() {
    await primed;
    return originalEnumerateDevices();
  };
})();

// Firefox lacks MediaStreamTrackProcessor / MediaStreamTrackGenerator (Chrome only "breakout box").
// The payload uses them to read raw AudioData / VideoFrame objects from tracks, so polyfill them.
(() => {
  if (typeof window.MediaStreamTrackProcessor !== "undefined") return;

  function audioReadable(track) {
    let ctx = null;
    let closed = false;
    const close = () => {
      closed = true;
      if (ctx) ctx.close().catch(() => {});
    };
    return new ReadableStream({
      start(controller) {
        ctx = new AudioContext();
        const source = ctx.createMediaStreamSource(new MediaStream([track]));
        // ScriptProcessor (not AudioWorklet) because worklet modules would be subject to the page's CSP
        const node = ctx.createScriptProcessor(1024, 1, 1);
        node.onaudioprocess = (e) => {
          if (closed) return;
          const data = new Float32Array(e.inputBuffer.getChannelData(0));
          try {
            controller.enqueue(
              new AudioData({
                format: "f32-planar",
                sampleRate: ctx.sampleRate,
                numberOfFrames: data.length,
                numberOfChannels: 1,
                timestamp: Math.round(performance.now() * 1000),
                data,
              })
            );
          } catch (err) {}
        };
        source.connect(node);
        node.connect(ctx.destination); // outputs silence, but required for onaudioprocess to fire
        track.addEventListener("ended", () => {
          close();
          try {
            controller.close();
          } catch (err) {}
        });
      },
      cancel: close,
    });
  }

  function rgbaToI420(rgba, width, height) {
    const ySize = width * height;
    const cw = width >> 1;
    const ch = height >> 1;
    const out = new Uint8Array(ySize + 2 * cw * ch);
    for (let i = 0, p = 0; i < ySize; i++, p += 4) {
      out[i] = (77 * rgba[p] + 150 * rgba[p + 1] + 29 * rgba[p + 2]) >> 8;
    }
    let u = ySize;
    let v = ySize + cw * ch;
    for (let y = 0; y < ch; y++) {
      for (let x = 0; x < cw; x++, u++, v++) {
        const p = ((y * 2) * width + x * 2) * 4;
        const r = rgba[p], g = rgba[p + 1], b = rgba[p + 2];
        out[u] = ((-43 * r - 85 * g + 128 * b) >> 8) + 128;
        out[v] = ((128 * r - 107 * g - 21 * b) >> 8) + 128;
      }
    }
    return out;
  }

  function videoReadable(track) {
    const video = document.createElement("video");
    video.muted = true;
    video.playsInline = true;
    video.srcObject = new MediaStream([track]);
    const canvas = document.createElement("canvas");
    const ctx2d = canvas.getContext("2d", { willReadFrequently: true });
    const MAX_FPS = 15;
    let lastFrameAt = 0;
    let closed = false;
    const close = () => {
      closed = true;
      video.srcObject = null;
    };
    return new ReadableStream(
      {
        start(controller) {
          video.play().catch(() => {});
          const onFrame = (now) => {
            if (closed) return;
            try {
              const width = video.videoWidth & ~1;
              const height = video.videoHeight & ~1;
              if (width && height && now - lastFrameAt >= 1000 / MAX_FPS && controller.desiredSize > 0) {
                lastFrameAt = now;
                canvas.width = width;
                canvas.height = height;
                ctx2d.drawImage(video, 0, 0, width, height);
                const rgba = ctx2d.getImageData(0, 0, width, height).data;
                controller.enqueue(
                  new VideoFrame(rgbaToI420(rgba, width, height), {
                    format: "I420",
                    codedWidth: width,
                    codedHeight: height,
                    timestamp: Math.round(now * 1000),
                  })
                );
              }
            } catch (err) {}
            video.requestVideoFrameCallback(onFrame);
          };
          video.requestVideoFrameCallback(onFrame);
          track.addEventListener("ended", () => {
            close();
            try {
              controller.close();
            } catch (err) {}
          });
        },
        cancel: close,
      },
      { highWaterMark: 2 }
    );
  }

  class MediaStreamTrackProcessor {
    constructor({ track }) {
      this.readable = track.kind === "audio" ? audioReadable(track) : videoReadable(track);
    }
  }

  // The payload only uses generators as sinks for frames it has already processed
  class MediaStreamTrackGenerator {
    constructor({ kind }) {
      this.kind = kind;
      this.writable = new WritableStream({
        write(frame) {
          frame?.close?.();
        },
      });
    }
  }

  window.MediaStreamTrackProcessor = MediaStreamTrackProcessor;
  window.MediaStreamTrackGenerator = MediaStreamTrackGenerator;
})();
