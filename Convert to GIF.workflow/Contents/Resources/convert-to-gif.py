#!/usr/bin/env python3

"""
Convert movies to GIF through gifski, driven from a local browser UI

gifski assigns every frame its own colour table, so gradients and glows
survive instead of collapsing into the stray pixels a single shared
palette produces. Its command line also decodes with ffmpeg, which
resolves ambiguous ProRes colour tags the same way Photoshop does, unlike
the Gifski app's AVFoundation path

The UI mirrors the Gifski app: trim, dimensions, speed, frame rate,
quality, loops and bounce, over a scrubbable preview

The script must live outside ~/Library/CloudStorage: macOS denies
Finder-invoked services read access to OneDrive's FileProvider domain
"""

import base64
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser

from argparse import ArgumentParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PREVIEW_WIDTH = 420
PREVIEW_FRAME_CAP = 240
ESTIMATE_FRAMES = 12
DEFAULT_FPS = 30
DEFAULT_QUALITY = 90
HEARTBEAT_INTERVAL = 2
HEARTBEAT_GRACE = 8
STARTUP_GRACE = 45


def notify(title, message):
    """
    Surfaces a message as a native alert

    @param title - Alert title
    @param message - Body text shown under the title
    """

    subprocess.run(
        [ 'osascript', '-e', f'display alert "{ title }" message "{ message }"' ],
        capture_output = True
    )


def require_binary(name):
    """
    Resolves an executable on PATH or raises with a readable install hint

    @param name - Executable to look for
    """

    found = shutil.which(name)

    if not found:
        raise RuntimeError(f"'{ name }' not found on PATH. Install it with: brew install { name }")

    return found


def run(command, failure):
    """
    Runs a command and raises with its stderr when it fails

    Bare CalledProcessError only carries an exit code, which says nothing
    about what actually went wrong inside ffmpeg or gifski

    @param command - Argument list to execute
    @param failure - Prefix for the error message
    """

    result = subprocess.run(command, capture_output = True, text = True)

    if result.returncode != 0:
        detail = ( result.stderr or '' ).strip().splitlines()

        raise RuntimeError(f'{ failure }: { detail[ -1 ] if detail else f"exit { result.returncode }" }')

    return result


def movie_info(path):
    """
    Reads the frame count, rate and dimensions of a movie

    @param path - Movie to inspect
    """

    result = subprocess.run(
        [ require_binary('ffprobe'), '-v', 'error', '-select_streams', 'v:0', '-count_frames',
          '-show_entries', 'stream=nb_read_frames,r_frame_rate,width,height',
          '-of', 'default=nw=1', str(path) ],
        capture_output = True, text = True
    )

    fields = dict(
        line.split('=', 1) for line in result.stdout.splitlines() if '=' in line
    )

    rate = fields.get('r_frame_rate', '30/1')
    numerator, _, denominator = rate.partition('/')
    fps = float(numerator) / float(denominator or 1)
    frames = int(fields.get('nb_read_frames', 0) or 0)

    return {
        'frames': max(frames, 1),
        'fps': round(fps, 3),
        'width': int(fields.get('width', 0) or 0),
        'height': int(fields.get('height', 0) or 0),
        'duration': round(max(frames, 1) / fps, 2) if fps else 0,
    }


def preview_frames(path, info, work):
    """
    Extracts every frame at preview size so scrubbing needs no round trip

    Long movies are strided down to a cap, which keeps the page payload
    reasonable while still covering the whole timeline

    @param path - Movie to read
    @param info - Result of movie_info
    @param work - Directory the frames are written to
    """

    stride = max(1, info[ 'frames' ] // PREVIEW_FRAME_CAP)
    selector = f"select='not(mod(n\\,{ stride }))'," if stride > 1 else ''

    run(
        [ require_binary('ffmpeg'), '-v', 'error', '-i', str(path),
          # ffmpeg 8 removed -vsync; passthrough keeps selected frames as-is
          '-vf', f'{ selector }scale={ PREVIEW_WIDTH }:-1', '-fps_mode', 'passthrough',
          str(work / 'p_%05d.png') ],
        'Could not extract preview frames'
    )

    frames = sorted(work.glob('p_*.png'))

    return [ base64.b64encode(frame.read_bytes()).decode() for frame in frames ], stride


def dominant_colour(path):
    """
    Returns the most common colour in a frame, as a hex string

    Used to seed the pin swatch, since the colour worth protecting from
    quantisation is almost always the flat background

    @param path - Image to sample
    """

    result = subprocess.run(
        [ require_binary('magick'), str(path), '-depth', '8', '-format', '%c', 'histogram:info:-' ],
        capture_output = True, text = True
    )

    best = ( 0, '#000000' )

    for line in result.stdout.splitlines():
        match = re.search(r'^\s*(\d+):.*(#[0-9A-Fa-f]{6})', line)

        if match and int(match.group(1)) > best[ 0 ]:
            best = ( int(match.group(1)), match.group(2).lower() )

    return best[ 1 ]


def trim_clip(source, destination, start, end, info):
    """
    Cuts the movie to the chosen range without re-encoding

    ProRes is all-intra, so a stream copy lands exactly on the requested
    frames and costs nothing in quality or time

    @param source - Movie to cut
    @param destination - Movie path to write
    @param start - First frame to keep
    @param end - Last frame to keep
    @param info - Result of movie_info
    """

    rate = info[ 'fps' ] or 30

    # Format specifiers cannot carry the usual padding inside the braces
    begin = format(start / rate, '.4f')
    stop = format(( end + 1 ) / rate, '.4f')

    run(
        [ require_binary('ffmpeg'), '-v', 'error', '-ss', begin,
          '-to', stop, '-i', str(source), '-c', 'copy', '-y', str(destination) ],
        'Could not trim the clip'
    )

    return destination


def gifski_command(source, destination, settings):
    """
    Builds the gifski invocation for the current settings

    @param source - Movie to encode
    @param destination - GIF path to write
    @param settings - Values collected from the UI
    """

    command = [
        require_binary('gifski'),
        '--output', str(destination),
        '--fps', str(settings.get('fps', DEFAULT_FPS)),
        '--quality', str(settings.get('quality', DEFAULT_QUALITY)),
        '--fast-forward', str(settings.get('speed', 1)),
        '--repeat', str(settings.get('repeat', 0)),
    ]

    if settings.get('width'):
        command += [ '--width', str(settings[ 'width' ]) ]

    if settings.get('height'):
        command += [ '--height', str(settings[ 'height' ]) ]

    if settings.get('bounce'):
        command.append('--bounce')

    # Pinning a colour guarantees it survives quantisation exactly, which
    # matters when a flat brand background has to stay on value
    if settings.get('fixed'):
        command += [ '--fixed-color', settings[ 'fixed' ].lstrip('#') ]

    if settings.get('fast'):
        command.append('--fast')

    command.append(str(source))

    return command


def encode(source, destination, settings, info, work):
    """
    Runs the full gifski encode, trimming first when a range is set

    @param source - Movie to convert
    @param destination - GIF path to write
    @param settings - Values collected from the UI
    @param info - Result of movie_info
    @param work - Directory for intermediates
    """

    clip = source
    start = int(settings.get('start', 0))
    end = int(settings.get('end', info[ 'frames' ] - 1))

    if start > 0 or end < info[ 'frames' ] - 1:
        clip = trim_clip(source, work / f'clip{ source.suffix }', start, end, info)

    result = subprocess.run(gifski_command(clip, destination, settings), capture_output = True, text = True)

    if result.returncode != 0 or not Path(destination).exists():
        raise RuntimeError(result.stderr.strip().splitlines()[ -1 ] if result.stderr.strip() else 'gifski failed')

    return destination


def estimate_size(source, settings, info, work):
    """
    Encodes a short sample and scales it up for a rough size figure

    @param source - Movie to sample
    @param settings - Values collected from the UI
    @param info - Result of movie_info
    @param work - Directory for intermediates
    """

    start = int(settings.get('start', 0))
    end = int(settings.get('end', info[ 'frames' ] - 1))
    span = max(1, end - start + 1)
    sample = min(ESTIMATE_FRAMES, span)

    clip = trim_clip(source, work / f'sample{ source.suffix }', start, start + sample - 1, info)
    target = work / 'sample.gif'

    target.unlink(missing_ok = True)

    probe = dict(settings)
    probe[ 'bounce' ] = False

    result = subprocess.run(gifski_command(clip, target, probe), capture_output = True, text = True)

    if result.returncode != 0 or not target.exists():
        return None

    rate = settings.get('fps', DEFAULT_FPS) / (info[ 'fps' ] or 30) / max(settings.get('speed', 1), 0.01)
    encoded = max(1, round(sample * rate))
    total = max(1, round(span * rate))
    scale = 2 if settings.get('bounce') else 1

    return round(target.stat().st_size / encoded * total * scale)


def build_page(name, info, frames, stride, pinned = '#000000'):
    """
    Renders the converter UI as a single self-contained page

    @param name - Filename shown in the header
    @param info - Result of movie_info
    @param frames - Base64 preview frames covering the timeline
    @param stride - Source frames represented by each preview frame
    @param pinned - Colour the pin swatch starts on
    """

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Convert to GIF</title>
    <style>
      :root {{
        --bg: #1e1e1e;
        --panel: #252526;
        --border: #3c3c3c;
        --text: #cccccc;
        --muted: #858585;
        --accent: #0a84ff;
      }}

      * {{ box-sizing: border-box }}

      body {{
        display: flex;
        flex-direction: column;
        gap: 12px;
        height: 100vh;
        margin: 0;
        padding: 14px;

        font-family: -apple-system, BlinkMacSystemFont, 'Helvetica Neue', sans-serif;
        font-size: 12px;
        color: var(--text);

        background: var(--bg);
      }}

      .gif__header {{
        display: flex;
        align-items: baseline;
        gap: 8px;
      }}

      .gif__title {{
        margin: 0;

        font-size: 13px;
        font-weight: 600;
      }}

      .gif__filename {{
        color: var(--muted);
      }}

      .gif__stage {{
        position: relative;

        display: flex;
        align-items: center;
        justify-content: center;
        flex: 1;
        min-height: 0;
        overflow: hidden;
        padding: 10px;

        cursor: grab;
        touch-action: none;

        border: 1px solid var(--border);
        border-radius: 6px;
        background-color: #2a2a2a;
        background-image:
          linear-gradient(45deg, #333 25%, transparent 25%),
          linear-gradient(-45deg, #333 25%, transparent 25%),
          linear-gradient(45deg, transparent 75%, #333 75%),
          linear-gradient(-45deg, transparent 75%, #333 75%);
        background-size: 16px 16px;
        background-position: 0 0, 0 8px, 8px -8px, -8px 0px;
      }}

      .gif__stage--panning {{
        cursor: grabbing;
      }}

      .gif__preview {{
        max-width: 100%;
        max-height: 100%;

        transform-origin: center center;
        will-change: transform;

        /* The stage owns every pointer gesture; without this the browser
           starts its own image drag and the pan never happens */
        pointer-events: none;
        user-select: none;
        -webkit-user-drag: none;
      }}

      /* Crisp pixels once magnified, so single-pixel artefacts stay visible */
      .gif__preview--magnified {{
        image-rendering: pixelated;
      }}

      .gif__transport {{
        display: flex;
        align-items: center;
        gap: 8px;
      }}

      .gif__scrub {{
        flex: 1;
        min-width: 0;

        accent-color: var(--accent);
      }}

      .gif__time {{
        flex: 0 0 auto;

        font-variant-numeric: tabular-nums;
        color: var(--muted);
      }}

      .gif__trim {{
        position: relative;

        height: 52px;
        overflow: hidden;

        border: 1px solid var(--border);
        border-radius: 6px;
        background: #151515;
        cursor: pointer;
        touch-action: none;
      }}

      .gif__strip {{
        display: block;

        width: 100%;
        height: 100%;
      }}

      .gif__mask {{
        position: absolute;
        top: 0;
        bottom: 0;

        background: rgba(12, 12, 12, 0.72);
        pointer-events: none;
      }}

      .gif__playhead {{
        position: absolute;
        top: 0;
        bottom: 0;

        width: 2px;
        margin-left: -1px;

        background: #ffffff;
        box-shadow: 0 0 4px rgba(0, 0, 0, 0.8);
        pointer-events: none;
      }}

      .gif__handle {{
        position: absolute;
        top: 0;
        bottom: 0;

        width: 10px;
        margin-left: -5px;

        border: 1px solid #7a5c00;
        border-radius: 3px;
        background: linear-gradient(#ffd351, #f0b400);
        cursor: ew-resize;
      }}

      .gif__handle::after {{
        position: absolute;
        top: 50%;
        left: 50%;

        width: 2px;
        height: 14px;
        margin: -7px 0 0 -1px;

        border-radius: 1px;
        background: rgba(0, 0, 0, 0.45);

        content: '';
      }}

      .gif__trimfields {{
        display: flex;
        align-items: center;
        gap: 8px;
      }}

      .gif__input--frame {{
        width: 62px;
      }}

      .gif__colour {{
        width: 40px;
        height: 24px;
        padding: 2px;

        border: 1px solid var(--border);
        border-radius: 5px;
        background: #1c1c1c;
        cursor: pointer;
      }}

      .gif__colour:disabled {{
        opacity: 0.4;
        cursor: default;
      }}

      .gif__panels {{
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 12px;
      }}

      .gif__panel {{
        display: flex;
        flex-direction: column;
        gap: 8px;
        padding: 10px 12px;

        border: 1px solid var(--border);
        border-radius: 6px;
        background: var(--panel);
      }}

      .gif__field {{
        display: flex;
        align-items: center;
        gap: 8px;
      }}

      .gif__key {{
        flex: 0 0 74px;

        text-align: right;
        color: var(--muted);
      }}

      .gif__value {{
        flex: 0 0 46px;

        font-variant-numeric: tabular-nums;
      }}

      .gif__slider {{
        flex: 1;
        min-width: 0;

        accent-color: var(--accent);
      }}

      .gif__input {{
        width: 74px;
        padding: 4px 6px;

        font-family: inherit;
        font-size: 12px;
        color: var(--text);

        border: 1px solid var(--border);
        border-radius: 5px;
        background: #1c1c1c;
      }}

      .gif__select {{
        flex: 1;
        min-width: 0;
        padding: 4px 6px;

        font-family: inherit;
        font-size: 12px;
        color: var(--text);

        border: 1px solid var(--border);
        border-radius: 5px;
        background: #1c1c1c;
      }}

      .gif__check {{
        display: flex;
        align-items: center;
        gap: 6px;

        accent-color: var(--accent);
      }}

      .gif__footer {{
        display: flex;
        align-items: center;
        gap: 10px;
      }}

      .gif__status {{
        flex: 1;

        color: var(--muted);
      }}

      .gif__button {{
        padding: 6px 16px;

        font-size: 12px;
        color: var(--text);

        border: 1px solid var(--border);
        border-radius: 6px;
        background: #333333;
        cursor: pointer;
      }}

      .gif__button--primary {{
        color: #ffffff;

        border-color: transparent;
        background: var(--accent);
      }}

      .gif__button:disabled {{
        opacity: 0.4;
        cursor: default;
      }}
    </style>
  </head>

  <body>
    <div class="gif__header">
      <h1 class="gif__title">Convert to GIF</h1>
      <span class="gif__filename">{ name }</span>
    </div>

    <div class="gif__stage">
      <img class="gif__preview" id="preview" alt="Preview" draggable="false" />
    </div>

    <div class="gif__transport">
      <button class="gif__button" id="play" type="button">Play</button>
      <input class="gif__scrub" id="scrub" type="range" min="0" max="{ max(len(frames) - 1, 0) }" value="0" />
      <span class="gif__time" id="time">00:00.00</span>
      <button class="gif__button" id="fit" type="button">Fit</button>
      <span class="gif__time" id="zoom">100%</span>
    </div>

    <div class="gif__trim" id="trim">
      <canvas class="gif__strip" id="strip"></canvas>
      <div class="gif__mask" id="mask-before"></div>
      <div class="gif__mask" id="mask-after"></div>
      <div class="gif__playhead" id="playhead"></div>
      <div class="gif__handle" id="handle-start"></div>
      <div class="gif__handle" id="handle-end"></div>
    </div>

    <div class="gif__trimfields">
      <span class="gif__key">In</span>
      <input class="gif__input gif__input--frame" id="start" type="number" min="0" max="{ max(info[ 'frames' ] - 1, 0) }" value="0" />
      <span class="gif__time" id="start-time">00:00.00</span>

      <span class="gif__key">Out</span>
      <input class="gif__input gif__input--frame" id="end" type="number" min="0" max="{ max(info[ 'frames' ] - 1, 0) }" value="{ max(info[ 'frames' ] - 1, 0) }" />
      <span class="gif__time" id="end-time">00:00.00</span>

      <span class="gif__status" id="range">full clip</span>
      <button class="gif__button" id="trim-reset" type="button">Reset trim</button>
    </div>

    <div class="gif__panels">
      <div class="gif__panel">
        <div class="gif__field">
          <span class="gif__key">Dimensions</span>
          <select class="gif__select" id="preset">
            <option value="100">{ info[ 'width' ] } x { info[ 'height' ] } (Original)</option>
            <option value="75">75%</option>
            <option value="50">50%</option>
            <option value="25">25%</option>
            <option value="custom">Custom</option>
          </select>
        </div>

        <div class="gif__field">
          <span class="gif__key">Width</span>
          <input class="gif__input" id="width" type="number" min="16" value="{ info[ 'width' ] }" />
          <span class="gif__key">Height</span>
          <input class="gif__input" id="height" type="number" min="16" value="{ info[ 'height' ] }" />
        </div>

        <div class="gif__field">
          <span class="gif__key">Speed</span>
          <input class="gif__slider" id="speed" type="range" min="0.25" max="4" step="0.25" value="1" />
          <span class="gif__value" id="speed-value">1x</span>
        </div>

        <div class="gif__field">
          <span class="gif__key">Pin colour</span>
          <input id="pin" type="checkbox" />
          <input class="gif__colour" id="fixed" type="color" value="{ pinned }" disabled />
          <span class="gif__time" id="pin-value">{ pinned }</span>
        </div>
      </div>

      <div class="gif__panel">
        <div class="gif__field">
          <span class="gif__key">FPS</span>
          <input class="gif__slider" id="fps" type="range" min="1" max="50" value="{ min(int(info[ 'fps' ]) or DEFAULT_FPS, 50) }" />
          <span class="gif__value" id="fps-value">{ min(int(info[ 'fps' ]) or DEFAULT_FPS, 50) }</span>
        </div>

        <div class="gif__field">
          <span class="gif__key">Quality</span>
          <input class="gif__slider" id="quality" type="range" min="1" max="100" value="{ DEFAULT_QUALITY }" />
          <span class="gif__value" id="quality-value">{ DEFAULT_QUALITY }</span>
        </div>

        <div class="gif__field">
          <span class="gif__key">Loops</span>
          <input class="gif__input" id="loops" type="number" min="1" value="1" disabled />
          <label class="gif__check"><input id="forever" type="checkbox" checked /> Loop forever</label>
        </div>

        <div class="gif__field">
          <span class="gif__key"></span>
          <label class="gif__check"><input id="bounce" type="checkbox" /> Bounce</label>
          <label class="gif__check"><input id="fast" type="checkbox" /> Fast encode</label>
        </div>
      </div>
    </div>

    <div class="gif__footer">
      <span class="gif__status" id="status">{ info[ 'frames' ] } frames at { info[ 'fps' ] }fps, { info[ 'duration' ] }s</span>
      <button class="gif__button" id="cancel" type="button">Cancel</button>
      <button class="gif__button gif__button--primary" id="convert" type="button">Convert</button>
    </div>

    <script>
      const frames = { json.dumps(frames) };
      const stride = { stride };
      const sourceFps = { info[ 'fps' ] or DEFAULT_FPS };
      const sourceFrames = { info[ 'frames' ] };
      const sourceWidth = { info[ 'width' ] };
      const sourceHeight = { info[ 'height' ] };

      const preview = document.getElementById('preview');
      const scrub = document.getElementById('scrub');
      const timeLabel = document.getElementById('time');
      const playButton = document.getElementById('play');
      const startInput = document.getElementById('start');
      const endInput = document.getElementById('end');
      const startTime = document.getElementById('start-time');
      const endTime = document.getElementById('end-time');
      const rangeLabel = document.getElementById('range');
      const trim = document.getElementById('trim');
      const strip = document.getElementById('strip');
      const stripContext = strip.getContext('2d');
      const maskBefore = document.getElementById('mask-before');
      const maskAfter = document.getElementById('mask-after');
      const playhead = document.getElementById('playhead');
      const handleStart = document.getElementById('handle-start');
      const handleEnd = document.getElementById('handle-end');

      const thumbs = [];

      let trimStart = 0;
      let trimEnd = sourceFrames - 1;
      let dragging = null;
      const preset = document.getElementById('preset');
      const widthInput = document.getElementById('width');
      const heightInput = document.getElementById('height');
      const loopsInput = document.getElementById('loops');
      const foreverBox = document.getElementById('forever');
      const status = document.getElementById('status');
      const convertButton = document.getElementById('convert');
      const stage = document.querySelector('.gif__stage');
      const zoomLabel = document.getElementById('zoom');
      const pinBox = document.getElementById('pin');
      const fixedInput = document.getElementById('fixed');
      const pinValue = document.getElementById('pin-value');

      const view = {{ scale: 1, x: 0, y: 0 }};

      let panning = null;
      let estimateTimer = null;
      let estimating = false;

      const sliders = [
        [ 'speed', value => `${{ value }}x` ],
        [ 'fps', value => value ],
        [ 'quality', value => value ]
      ];

      let playing = false;
      let timer = null;
      let closing = false;

      /**
       * Formats a source frame index as mm:ss.hh
       *
       * @param index - Frame number in the source movie
       */
      function stamp(index) {{
        const seconds = index / sourceFps;
        const minutes = Math.floor(seconds / 60);
        const rest = seconds - minutes * 60;

        return `${{ String(minutes).padStart(2, '0') }}:${{ rest.toFixed(2).padStart(5, '0') }}`;
      }}

      /**
       * Shows the preview frame at the current scrub position
       */
      function draw() {{
        const index = Number(scrub.value);

        preview.src = `data:image/png;base64,${{ frames[ index ] }}`;
        timeLabel.textContent = stamp(index * stride);
        playhead.style.left = `${{ frameToX(index * stride) }}px`;
      }}

      /**
       * Steps playback forward, wrapping inside the trimmed range
       */
      function step() {{
        const first = Math.floor(trimStart / stride);
        const last = Math.floor(trimEnd / stride);
        let next = Number(scrub.value) + 1;

        if (next > last || next >= frames.length) {{
          next = first;
        }}

        scrub.value = String(next);
        draw();
      }}

      /**
       * Collects the current settings for the server
       */
      function settings() {{
        return {{
          start: trimStart,
          end: trimEnd,
          fps: Number(document.getElementById('fps').value),
          quality: Number(document.getElementById('quality').value),
          speed: Number(document.getElementById('speed').value),
          width: Number(widthInput.value) || null,
          height: Number(heightInput.value) || null,
          repeat: foreverBox.checked ? 0 : Math.max(Number(loopsInput.value) || 1, 1),
          bounce: document.getElementById('bounce').checked,
          fast: document.getElementById('fast').checked,
          fixed: pinBox.checked ? fixedInput.value : ''
        }};
      }}

      sliders.forEach(([ id, format ]) => {{
        const input = document.getElementById(id);
        const label = document.getElementById(`${{ id }}-value`);

        input.addEventListener('input', () => {{ label.textContent = format(input.value); }});
      }});

      scrub.addEventListener('input', () => {{
        if (playing) {{
          clearInterval(timer);
          playing = false;
          playButton.textContent = 'Play';
        }}

        draw();
      }});

      playButton.addEventListener('click', () => {{
        playing = !playing;
        playButton.textContent = playing ? 'Pause' : 'Play';

        clearInterval(timer);

        if (playing) {{
          timer = setInterval(step, 1_000 / (sourceFps / stride));
        }}
      }});

      /**
       * Maps a source frame to its horizontal position on the strip
       *
       * @param frame - Frame number in the source movie
       */
      function frameToX(frame) {{
        return sourceFrames < 2 ? 0 : frame / (sourceFrames - 1) * trim.clientWidth;
      }}

      /**
       * Maps a horizontal position on the strip back to a source frame
       *
       * @param x - Offset in pixels from the strip's left edge
       */
      function xToFrame(x) {{
        const span = trim.clientWidth || 1;

        return Math.max(0, Math.min(sourceFrames - 1, Math.round(x / span * (sourceFrames - 1))));
      }}

      /**
       * Shows the preview frame nearest a given source frame
       *
       * @param frame - Frame number in the source movie
       */
      function showSourceFrame(frame) {{
        const index = Math.max(0, Math.min(frames.length - 1, Math.round(frame / stride)));

        scrub.value = String(index);
        draw();
      }}

      /**
       * Paints thumbnails across the strip, centre-cropped so several fit
       *
       * A wide banner scaled to full aspect would be only a few pixels tall,
       * so each thumbnail takes a centre slice instead
       */
      function drawStrip() {{
        const width = trim.clientWidth;
        const height = trim.clientHeight;
        const ratio = window.devicePixelRatio || 1;

        if (!width || !height) {{
          return;
        }}

        strip.width = Math.round(width * ratio);
        strip.height = Math.round(height * ratio);
        stripContext.setTransform(ratio, 0, 0, ratio, 0, 0);
        stripContext.clearRect(0, 0, width, height);

        const slot = 58;
        const count = Math.max(1, Math.round(width / slot));
        const slotWidth = width / count;

        for (let i = 0; i < count; i += 1) {{
          const position = count === 1 ? 0 : i / (count - 1);
          const image = thumbs[ Math.round(position * (thumbs.length - 1)) ];

          if (!image || !image.complete || !image.naturalWidth) {{
            continue;
          }}

          const sliceWidth = Math.min(image.naturalWidth, image.naturalHeight * (slotWidth / height));
          const sliceX = (image.naturalWidth - sliceWidth) / 2;

          stripContext.drawImage(image, sliceX, 0, sliceWidth, image.naturalHeight, i * slotWidth, 0, slotWidth + 0.5, height);
        }}
      }}

      /**
       * Positions the masks, handles and playhead from the current state
       */
      function layout() {{
        const left = frameToX(trimStart);
        const right = frameToX(trimEnd);

        maskBefore.style.left = '0px';
        maskBefore.style.width = `${{ left }}px`;
        maskAfter.style.left = `${{ right }}px`;
        maskAfter.style.width = `${{ Math.max(0, trim.clientWidth - right) }}px`;
        handleStart.style.left = `${{ left }}px`;
        handleEnd.style.left = `${{ right }}px`;
        playhead.style.left = `${{ frameToX(Number(scrub.value) * stride) }}px`;

        startInput.value = String(trimStart);
        endInput.value = String(trimEnd);
        startTime.textContent = stamp(trimStart);
        endTime.textContent = stamp(trimEnd);

        const kept = trimEnd - trimStart + 1;

        rangeLabel.textContent = trimStart === 0 && trimEnd === sourceFrames - 1
          ? `full clip, ${{ sourceFrames }} frames`
          : `${{ kept }} of ${{ sourceFrames }} frames, ${{ (kept / sourceFps).toFixed(2) }}s`;
      }}

      /**
       * Applies a drag or click to whichever control is active
       *
       * @param what - One of start, end or scrub
       * @param frame - Source frame the pointer is over
       */
      function applyDrag(what, frame) {{
        if (what === 'start') {{
          trimStart = Math.min(frame, trimEnd);
          showSourceFrame(trimStart);
        }}

        else if (what === 'end') {{
          trimEnd = Math.max(frame, trimStart);
          showSourceFrame(trimEnd);
        }}

        else {{
          showSourceFrame(frame);
        }}

        layout();
      }}

      trim.addEventListener('pointerdown', event => {{
        const bounds = trim.getBoundingClientRect();
        const x = event.clientX - bounds.left;
        const toStart = Math.abs(x - frameToX(trimStart));
        const toEnd = Math.abs(x - frameToX(trimEnd));

        dragging = Math.min(toStart, toEnd) > 12 ? 'scrub' : (toStart <= toEnd ? 'start' : 'end');

        trim.setPointerCapture(event.pointerId);
        applyDrag(dragging, xToFrame(x));
      }});

      trim.addEventListener('pointermove', event => {{
        if (!dragging) {{
          return;
        }}

        const bounds = trim.getBoundingClientRect();

        applyDrag(dragging, xToFrame(event.clientX - bounds.left));
      }});

      [ 'pointerup', 'pointercancel' ].forEach(name => trim.addEventListener(name, () => {{
        if (dragging === 'start' || dragging === 'end') {{
          scheduleEstimate();
        }}

        dragging = null;
      }}));

      [ startInput, endInput ].forEach(input => input.addEventListener('change', () => {{
        const value = Math.max(0, Math.min(sourceFrames - 1, Number(input.value) || 0));

        if (input === startInput) {{
          trimStart = Math.min(value, trimEnd);
          showSourceFrame(trimStart);
        }}

        else {{
          trimEnd = Math.max(value, trimStart);
          showSourceFrame(trimEnd);
        }}

        layout();
        scheduleEstimate();
      }}));

      document.getElementById('trim-reset').addEventListener('click', () => {{
        trimStart = 0;
        trimEnd = sourceFrames - 1;
        layout();
        scheduleEstimate();
      }});

      window.addEventListener('resize', () => {{
        drawStrip();
        layout();
      }});

      preset.addEventListener('change', () => {{
        if (preset.value === 'custom') {{
          return;
        }}

        const factor = Number(preset.value) / 100;

        widthInput.value = String(Math.round(sourceWidth * factor));
        heightInput.value = String(Math.round(sourceHeight * factor));
      }});

      [ widthInput, heightInput ].forEach(input => input.addEventListener('input', () => {{
        preset.value = 'custom';
      }}));

      foreverBox.addEventListener('change', () => {{
        loopsInput.disabled = foreverBox.checked;
      }});

      pinBox.addEventListener('change', () => {{
        fixedInput.disabled = !pinBox.checked;
      }});

      fixedInput.addEventListener('input', () => {{
        pinValue.textContent = fixedInput.value;
      }});

      /**
       * Applies the current pan and zoom to the preview
       */
      function applyView() {{
        preview.style.transform = `translate(${{ view.x }}px, ${{ view.y }}px) scale(${{ view.scale }})`;
        preview.classList.toggle('gif__preview--magnified', view.scale > 1.5);
        zoomLabel.textContent = `${{ Math.round(view.scale * 100) }}%`;
      }}

      stage.addEventListener('wheel', event => {{
        event.preventDefault();

        const bounds = stage.getBoundingClientRect();
        const next = Math.max(0.2, Math.min(24, view.scale * Math.exp(-event.deltaY * 0.0018)));
        const ratio = next / view.scale;

        // Keep whatever sits under the cursor pinned while the scale changes
        const x = event.clientX - bounds.left - bounds.width / 2;
        const y = event.clientY - bounds.top - bounds.height / 2;

        view.x = x - (x - view.x) * ratio;
        view.y = y - (y - view.y) * ratio;
        view.scale = next;

        applyView();
      }}, {{ passive: false }});

      stage.addEventListener('pointerdown', event => {{
        panning = {{ x: event.clientX, y: event.clientY }};
        stage.classList.add('gif__stage--panning');
        stage.setPointerCapture(event.pointerId);
      }});

      stage.addEventListener('pointermove', event => {{
        if (!panning) {{
          return;
        }}

        view.x += event.clientX - panning.x;
        view.y += event.clientY - panning.y;
        panning = {{ x: event.clientX, y: event.clientY }};

        applyView();
      }});

      [ 'pointerup', 'pointercancel' ].forEach(name => stage.addEventListener(name, () => {{
        panning = null;
        stage.classList.remove('gif__stage--panning');
      }}));

      stage.addEventListener('dblclick', () => {{
        view.scale = 1;
        view.x = 0;
        view.y = 0;
        applyView();
      }});

      document.getElementById('fit').addEventListener('click', () => {{
        view.scale = 1;
        view.x = 0;
        view.y = 0;
        applyView();
      }});

      /**
       * Re-estimates the output size a beat after the settings settle
       *
       * Each estimate encodes a real sample, so it waits for typing and
       * slider drags to finish rather than firing on every keystroke
       */
      function scheduleEstimate() {{
        clearTimeout(estimateTimer);
        status.textContent = 'Settings changed';
        estimateTimer = setTimeout(runEstimate, 1_000);
      }}

      /**
       * Encodes a sample and reports the projected file size
       */
      async function runEstimate() {{
        if (estimating) {{
          scheduleEstimate();

          return;
        }}

        estimating = true;
        status.textContent = 'Estimating size';

        try {{
          const response = await fetch('/estimate', {{ method: 'POST', body: JSON.stringify(settings()) }});
          const result = await response.json();

          status.textContent = result.message;
        }}

        catch (error) {{
          status.textContent = 'Could not estimate size';
        }}

        finally {{
          estimating = false;
        }}
      }}

      convertButton.addEventListener('click', async () => {{
        convertButton.disabled = true;
        status.textContent = 'Converting with gifski';

        const response = await fetch('/convert', {{ method: 'POST', body: JSON.stringify(settings()) }});
        const result = await response.json();

        status.textContent = result.message;

        if (result.ok) {{
          closing = true;
          setTimeout(() => window.close(), 1_500);
        }}

        else {{
          convertButton.disabled = false;
        }}
      }});

      document.getElementById('cancel').addEventListener('click', () => {{
        closing = true;
        navigator.sendBeacon('/cancel');
        window.close();
      }});

      setInterval(() => fetch('/ping', {{ method: 'POST' }}).catch(() => {{}}), { HEARTBEAT_INTERVAL * 1_000 });

      window.addEventListener('pagehide', () => {{
        if (!closing) {{
          navigator.sendBeacon('/cancel');
        }}
      }});

      // Every setting feeds the same debounced estimate, so the projected
      // size stays in step with the controls without a button
      [ 'speed', 'fps', 'quality', 'preset', 'width', 'height', 'loops', 'forever', 'bounce', 'fast', 'pin', 'fixed' ]
        .forEach(id => {{
          const input = document.getElementById(id);

          input.addEventListener('input', scheduleEstimate);
          input.addEventListener('change', scheduleEstimate);
        }});

      // Thumbnails decode in the background; the strip repaints as they land
      frames.forEach(data => {{
        const image = new Image();

        image.onload = drawStrip;
        image.src = `data:image/png;base64,${{ data }}`;
        thumbs.push(image);
      }});

      draw();
      drawStrip();
      layout();
      applyView();
      runEstimate();
    </script>
  </body>
</html>"""


def serve(source, info, work):
    """
    Runs the converter UI and blocks until the user converts or closes it

    @param source - Movie being converted
    @param info - Result of movie_info
    @param work - Directory for preview frames and intermediates
    """

    frames, stride = preview_frames(source, info, work)
    staged = sorted(work.glob('p_*.png'))
    pinned = dominant_colour(staged[ len(staged) // 2 ]) if staged else '#000000'
    page = build_page(source.name, info, frames, stride, pinned).encode()

    finished = threading.Event()
    outcome = {}
    heartbeat = { 'seen': None }

    # ffmpeg and gifski both saturate the machine, and the intermediates are
    # shared files, so encodes run one at a time
    encoder = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        """
        Serves the converter page, size estimates and the final encode
        """

        def log_message(self, *args):
            """
            Silences the default request logging so the terminal stays clean
            """

            pass

        def respond(self, status, body, content_type):
            """
            Writes a complete response in one go

            @param status - HTTP status code
            @param body - Raw response body
            @param content_type - MIME type of the body
            """

            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def read_json(self):
            """
            Reads and decodes the JSON payload attached to the request
            """

            length = int(self.headers.get('Content-Length', 0))

            return json.loads(self.rfile.read(length) or '{}')

        def do_GET(self):
            """
            Serves the converter page
            """

            if self.path == '/':
                self.respond(200, page, 'text/html; charset=utf-8')

                return

            self.respond(404, b'', 'text/plain')

        def do_POST(self):
            """
            Handles heartbeats, size estimates and the conversion
            """

            if self.path == '/ping':
                heartbeat[ 'seen' ] = time.monotonic()
                self.respond(204, b'', 'text/plain')

                return

            if self.path == '/cancel':
                self.respond(204, b'', 'text/plain')
                finished.set()

                return

            payload = self.read_json()

            if self.path == '/estimate':
                try:
                    with encoder:
                        size = estimate_size(source, payload, info, work)

                    body = {
                        'ok': size is not None,
                        'message': f'Estimated { size // 1_024 }KB' if size else 'Could not estimate'
                    }

                except (RuntimeError, subprocess.CalledProcessError) as error:
                    body = { 'ok': False, 'message': str(error) }

                self.respond(200, json.dumps(body).encode(), 'application/json')

                return

            if self.path == '/convert':
                destination = source.with_suffix('.gif')

                try:
                    with encoder:
                        encode(source, destination, payload, info, work)

                    size = destination.stat().st_size // 1_024
                    outcome.update({ 'ok': True, 'message': f'Saved { destination.name } ({ size }KB)' })

                except (RuntimeError, subprocess.CalledProcessError) as error:
                    outcome.update({ 'ok': False, 'message': str(error) })

                self.respond(200, json.dumps(outcome).encode(), 'application/json')

                if outcome.get('ok'):
                    finished.set()

                return

            self.respond(404, b'', 'text/plain')

    with socket.socket() as probe:
        probe.bind(( '127.0.0.1', 0 ))
        port = probe.getsockname()[ 1 ]

    server = ThreadingHTTPServer(( '127.0.0.1', port ), Handler)

    threading.Thread(target = server.serve_forever, daemon = True).start()

    def watchdog():
        """
        Ends the session once the page stops sending heartbeats

        The beacon fired on window close never arrives if the browser is
        force quit, so a missed-heartbeat timeout is what guarantees the
        script exits instead of blocking forever
        """

        started = time.monotonic()

        while not finished.wait(HEARTBEAT_INTERVAL):
            seen = heartbeat[ 'seen' ]

            if seen is None:
                if time.monotonic() - started > STARTUP_GRACE:
                    outcome.update({ 'ok': False, 'message': 'Browser window never opened, giving up' })
                    finished.set()

                continue

            # A long encode blocks its request thread well past the grace
            # period, so only idle sessions are timed out
            if time.monotonic() - seen > HEARTBEAT_GRACE and not encoder.locked():
                finished.set()

    url = f'http://127.0.0.1:{ port }/'
    binary = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')

    threading.Thread(target = watchdog, daemon = True).start()

    if binary.exists():
        profile = Path.home() / 'Library/Caches/convert-to-gif-chrome'

        subprocess.Popen(
            [ str(binary), f'--app={ url }', f'--user-data-dir={ profile }', '--window-size=820,880', '--no-first-run', '--no-default-browser-check' ],
            stdout = subprocess.DEVNULL, stderr = subprocess.DEVNULL
        )

    else:
        webbrowser.open(url)

    print(f'Converter open at { url } - close the window to cancel', flush = True)

    try:
        finished.wait()

    except KeyboardInterrupt:
        pass

    server.shutdown()

    return outcome


def process(path):
    """
    Opens the converter UI for one movie

    @param path - Movie to convert
    """

    source = Path(path).resolve()

    if not source.exists():
        print(f"Warning: '{ source }' not found, skipping")

        return

    info = movie_info(source)

    print(f'Reading { source.name } ({ info[ "frames" ] } frames at { info[ "fps" ] }fps)')

    with tempfile.TemporaryDirectory() as work:
        outcome = serve(source, info, Path(work))

    if outcome:
        print(outcome[ 'message' ])

    else:
        print('Cancelled')


def main():
    """
    Parses arguments and opens the converter for each movie
    """

    parser = ArgumentParser(description = 'Convert movies to GIF with gifski')

    parser.add_argument('files', nargs = '+', help = 'movie files to convert')
    parser.add_argument('--alert', action = 'store_true', help = 'report failures as a native alert rather than on stderr')

    arguments = parser.parse_args()

    for path in arguments.files:
        try:
            process(path)

        except (Exception, SystemExit) as error:
            detail = str(error) or error.__class__.__name__

            print(f'Error on { Path(path).name }: { detail }', file = sys.stderr, flush = True)

            if arguments.alert:
                notify('Convert to GIF', f'{ Path(path).name }\\n\\n{ detail }'.replace('"', "'"))


if __name__ == '__main__':
    main()
