#!/usr/bin/env python3

"""
Remove colours from animated GIFs through a local browser picker

Samples a palette from the source GIF, serves a GUI for choosing which
colours to strip, previews the result on a single frame and as a playable
animation, then applies the removal across every frame with ImageMagick
and re-optimises the output with gifsicle

Movie inputs are handed to Photoshop via mov-to-gif.sh first, so the whole
chain runs Photoshop then ImageMagick then gifsicle

The script must live outside ~/Library/CloudStorage: macOS denies
Finder-invoked services read access to OneDrive's FileProvider domain
"""

import base64
import json
import os
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

MOVIE_SUFFIXES = { '.mov', '.mp4', '.m4v' }
PALETTE_SAMPLE_FRAMES = 6
PREVIEW_WIDTH = 460
ANIMATION_WIDTH = 320
ANIMATION_LOOPS = 2
DEFAULT_TOLERANCE = 8
DEFAULT_THRESHOLD = 24
DEFAULT_DISPOSAL = 'Background'
HEARTBEAT_INTERVAL = 2
HEARTBEAT_GRACE = 8
STARTUP_GRACE = 30

CONVERTER_CANDIDATES = (
    Path(__file__).resolve().parent / 'mov-to-gif.sh',
    Path.home() / 'Library/CloudStorage/OneDrive-BondBrandLoyalty,Inc/Scripts/mov-to-gif.sh',
)


def notify(title, message):
    """
    Surfaces a message as a native alert

    The Quick Action detaches this script so it outlives the Automator
    action, which means nothing upstream is left to report failures

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


def frame_count(path):
    """
    Counts the frames in a GIF so the palette sampler can spread across it

    @param path - GIF to inspect
    """

    result = subprocess.run(
        [ require_binary('gifsicle'), '--info', str(path) ],
        capture_output = True, text = True
    )

    match = re.search(r'(\d+) images', result.stdout)

    return int(match.group(1)) if match else 1


def sample_palette(path):
    """
    Builds a pixel-frequency palette by sampling frames across the animation

    Sampling rather than reading every frame keeps startup near-instant on
    long animations, while still catching colours that only appear later on

    @param path - GIF to sample
    """

    total = frame_count(path)
    step = max(1, total // PALETTE_SAMPLE_FRAMES)
    indexes = list(range(0, total, step))[ :PALETTE_SAMPLE_FRAMES ]
    counts = {}

    for index in indexes:
        result = subprocess.run(
            [ require_binary('magick'), f'{ path }[{ index }]', '-coalesce', '-depth', '8', '-format', '%c', 'histogram:info:-' ],
            capture_output = True, text = True
        )

        for line in result.stdout.splitlines():
            match = re.search(r'^\s*(\d+):.*(#[0-9A-Fa-f]{6})', line)

            if match:
                colour = match.group(2).upper()
                counts[ colour ] = counts.get(colour, 0) + int(match.group(1))

    total_pixels = sum(counts.values()) or 1

    return [
        { 'hex': colour, 'share': round(count / total_pixels * 100, 3) }
        for colour, count in sorted(counts.items(), key = lambda item: item[ 1 ], reverse = True)
    ]


def removal_arguments(colours, tolerance, mode = 'transparent', target = '#000000'):
    """
    Expands a colour selection into repeated ImageMagick fuzzy-match flags

    Each colour needs its own fuzz pairing because ImageMagick applies the
    current fuzz value at the moment the operator runs

    Snapping exists for footage whose faint gradients quantise into stray
    pixels: the near-matches collapse onto one flat colour instead of being
    cut out, which clears the dust a glow leaves behind

    @param colours - Hex colours to strip
    @param tolerance - Fuzzy match percentage applied to each colour
    @param mode - Either transparent to cut colours out, or snap to flatten them
    @param target - Colour that snapped pixels become
    """

    arguments = []

    for colour in colours:
        arguments += [ '-fuzz', f'{ tolerance }%' ]

        if mode == 'snap':
            arguments += [ '-fill', target, '-opaque', colour ]

        else:
            arguments += [ '-transparent', colour ]

    return arguments


def disposal_arguments(disposal):
    """
    Builds the frame disposal flags for the chosen mode

    Disposal decides what a frame leaves behind for the next one. Once
    colours are stripped the frames carry transparency, so leaving disposal
    as authored lets each pass paint over the last and the animation
    visibly stacks on its second loop

    @param disposal - One of Background, None, Previous or Auto
    """

    if disposal == 'Auto':
        return []

    return [ '-set', 'dispose', disposal ]


def representative_frame(path):
    """
    Picks the frame with the most visible content to preview

    Animations that fade in start on a fully transparent frame, so frame
    zero often shows nothing at all and makes the picker look broken

    @param path - GIF to inspect
    """

    magick = require_binary('magick')
    total = frame_count(path)
    step = max(1, total // PALETTE_SAMPLE_FRAMES)
    best = ( 0, -1.0 )

    for index in list(range(0, total, step))[ :PALETTE_SAMPLE_FRAMES ]:
        result = subprocess.run(
            [ magick, f'{ path }[{ index }]', '-coalesce', '-format', '%[fx:mean.a]', 'info:' ],
            capture_output = True, text = True
        )

        try:
            coverage = float(result.stdout.strip() or 0)

        except ValueError:
            coverage = 0.0

        if coverage > best[ 1 ]:
            best = ( index, coverage )

    return best[ 0 ]


def render_preview(path, colours, tolerance, index = 0, mode = 'transparent', target = '#000000'):
    """
    Renders a single frame with the removal applied, for the live preview

    @param path - Source GIF
    @param colours - Hex colours to strip
    @param tolerance - Fuzzy match percentage
    @param index - Frame to render
    @param mode - Either transparent or snap
    @param target - Colour that snapped pixels become
    """

    command = [ require_binary('magick'), f'{ path }[{ index }]', '-coalesce' ]
    command += removal_arguments(colours, tolerance, mode, target)
    command += [ '-resize', f'{ PREVIEW_WIDTH }x', 'png:-' ]

    result = subprocess.run(command, capture_output = True)

    return result.stdout


def render_animation(path, colours, tolerance, disposal, mode = 'transparent', target = '#000000'):
    """
    Builds a reduced-size preview animation and returns its frames

    The sequence is repeated before coalescing so playback runs past the
    loop point, which is the only place bad disposal becomes visible

    @param path - Source GIF
    @param colours - Hex colours to strip
    @param tolerance - Fuzzy match percentage
    @param disposal - Frame disposal mode to preview
    @param mode - Either transparent or snap
    @param target - Colour that snapped pixels become
    """

    magick = require_binary('magick')

    with tempfile.TemporaryDirectory() as work:
        staged = Path(work) / 'staged.gif'

        command = [ magick, str(path), '-coalesce' ]
        command += removal_arguments(colours, tolerance, mode, target)
        command += [ '-resize', f'{ ANIMATION_WIDTH }x' ]
        command += disposal_arguments(disposal)
        command += [ '-layers', 'optimize', str(staged) ]

        subprocess.run(command, capture_output = True, check = True)

        delays = subprocess.run(
            [ magick, 'identify', '-format', '%T\n', str(staged) ],
            capture_output = True, text = True
        ).stdout.split()

        subprocess.run(
            [ magick ] + [ str(staged) ] * ANIMATION_LOOPS + [ '-coalesce', str(Path(work) / 'frame_%04d.png') ],
            capture_output = True, check = True
        )

        frames = [
            base64.b64encode(frame.read_bytes()).decode()
            for frame in sorted(Path(work).glob('frame_*.png'))
        ]

    # Delays are centiseconds in the GIF header and browsers floor very
    # short ones, so anything under 2cs is nudged to the usual 10cs default
    timings = [ max(int(delay), 2) * 10 for delay in delays ] or [ 100 ]

    return { 'frames': frames, 'delays': timings * ANIMATION_LOOPS }


def apply_removal(source, destination, colours, tolerance, disposal, mode = 'transparent', target = '#000000'):
    """
    Strips the chosen colours across every frame, then re-optimises

    @param source - Source GIF
    @param destination - Path to write the processed GIF to
    @param colours - Hex colours to strip
    @param tolerance - Fuzzy match percentage
    @param disposal - Frame disposal mode to write
    @param mode - Either transparent or snap
    @param target - Colour that snapped pixels become
    """

    command = [ require_binary('magick'), str(source), '-coalesce' ]
    command += removal_arguments(colours, tolerance, mode, target)
    command += disposal_arguments(disposal)
    command += [ '-layers', 'optimize', str(destination) ]

    result = subprocess.run(command, capture_output = True, text = True)

    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or 'ImageMagick failed')

    optimised = subprocess.run(
        [ require_binary('gifsicle'), '-O3', str(destination), '-o', str(destination) ],
        capture_output = True, text = True
    )

    if optimised.returncode != 0:
        raise RuntimeError(optimised.stderr.strip() or 'gifsicle failed')

    return destination


def convert_movie(path):
    """
    Hands a movie to Photoshop via mov-to-gif.sh and returns the GIF it wrote

    @param path - Movie file to convert
    """

    converter = None

    for candidate in CONVERTER_CANDIDATES:
        # Readability matters more than existence here, because macOS denies
        # Finder-invoked services access to CloudStorage even when the file is there
        try:
            candidate.open('rb').close()
            converter = candidate

            break

        except OSError:
            continue

    if not converter:
        locations = ' or '.join(str(candidate) for candidate in CONVERTER_CANDIDATES)

        raise RuntimeError(f'mov-to-gif.sh is not readable at { locations }. Copy it next to this script to convert movies from Finder')

    subprocess.run([ 'bash', str(converter), str(path) ], check = True)
    produced = path.with_suffix('.gif')

    if not produced.exists():
        raise RuntimeError('Photoshop did not produce a GIF')

    return produced


def build_page(name, palette, preview):
    """
    Renders the picker GUI as a single self-contained page

    @param name - Filename shown in the header
    @param palette - Sampled colours with their pixel share
    @param preview - Base64 PNG of the unmodified first frame
    """

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Remove Colours</title>
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
        gap: 14px;
        height: 100vh;
        margin: 0;
        padding: 16px;

        font-family: -apple-system, BlinkMacSystemFont, 'Helvetica Neue', sans-serif;
        font-size: 13px;
        color: var(--text);

        background: var(--bg);
      }}

      .picker__header {{
        display: flex;
        align-items: baseline;
        gap: 8px;
      }}

      .picker__title {{
        margin: 0;

        font-size: 14px;
        font-weight: 600;
      }}

      .picker__filename {{
        color: var(--muted);
      }}

      .picker__body {{
        display: grid;
        grid-template-columns: 1fr 320px;
        gap: 16px;
        flex: 1;
        min-height: 0;
      }}

      .picker__viewer {{
        display: flex;
        flex-direction: column;
        gap: 10px;
        min-width: 0;
      }}

      .picker__stage {{
        display: flex;
        align-items: center;
        justify-content: center;
        flex: 1;
        min-height: 0;
        padding: 12px;

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

      .picker__preview,
      .picker__canvas {{
        max-width: 100%;
        max-height: 100%;

        image-rendering: pixelated;
      }}

      .picker__transport {{
        display: flex;
        align-items: center;
        gap: 8px;
      }}

      .picker__scrub {{
        flex: 1;

        accent-color: var(--accent);
      }}

      .picker__sidebar {{
        display: flex;
        flex-direction: column;
        gap: 10px;
        min-height: 0;
      }}

      .picker__label {{
        display: flex;
        justify-content: space-between;

        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        color: var(--muted);
      }}

      .picker__row {{
        display: flex;
        gap: 6px;
      }}

      .picker__swatches {{
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(26px, 1fr));
        gap: 4px;
        flex: 3 1 0;
        overflow-y: auto;
        min-height: 90px;
        padding: 8px;

        border: 1px solid var(--border);
        border-radius: 6px;
        background: var(--panel);
      }}

      .picker__swatch {{
        position: relative;

        aspect-ratio: 1;
        padding: 0;

        border: 1px solid rgba(255, 255, 255, 0.14);
        border-radius: 4px;
        cursor: pointer;

        transition: transform 0.08s ease;
      }}

      .picker__swatch:hover {{
        transform: scale(1.12);
      }}

      .picker__swatch--active {{
        border: 2px solid var(--accent);
        box-shadow: 0 0 0 2px rgba(10, 132, 255, 0.35);
      }}

      .picker__swatch--active::after {{
        position: absolute;
        inset: 0;

        display: flex;
        align-items: center;
        justify-content: center;

        font-size: 14px;
        font-weight: 700;
        color: #ffffff;
        text-shadow: 0 0 3px rgba(0, 0, 0, 0.9);

        content: '\\00d7';
      }}

      .picker__select {{
        flex: 0 0 auto;
        width: 100%;
        min-width: 0;
        padding: 5px 8px;

        font-family: inherit;
        font-size: 12px;
        color: var(--text);

        border: 1px solid var(--border);
        border-radius: 6px;
        background: var(--panel);
      }}

      /* A select only stretches horizontally inside the sort row */
      .picker__row .picker__select {{
        flex: 1;
        width: auto;
      }}

      .picker__colour {{
        flex: 0 0 auto;
        width: 40px;
        height: 28px;
        padding: 2px;

        border: 1px solid var(--border);
        border-radius: 6px;
        background: var(--panel);
        cursor: pointer;
      }}

      .picker__colour:disabled {{
        opacity: 0.4;
        cursor: default;
      }}

      .picker__spacer {{
        flex: 1 1 0;
        min-height: 8px;
      }}

      .picker__slider {{
        width: 100%;

        accent-color: var(--accent);
      }}

      .picker__button {{
        flex: 1;
        padding: 7px 12px;

        font-size: 13px;
        color: var(--text);

        border: 1px solid var(--border);
        border-radius: 6px;
        background: #333333;
        cursor: pointer;
      }}

      .picker__button--icon {{
        flex: 0 0 auto;
        min-width: 38px;
        padding: 5px 10px;
      }}

      .picker__button--primary {{
        color: #ffffff;

        border-color: transparent;
        background: var(--accent);
      }}

      .picker__button--on {{
        color: #ffffff;

        border-color: var(--accent);
      }}

      .picker__button:disabled {{
        opacity: 0.4;
        cursor: default;
      }}

      .picker__status {{
        min-height: 15px;

        font-size: 11px;
        color: var(--muted);
      }}
    </style>
  </head>

  <body>
    <div class="picker__header">
      <h1 class="picker__title">Remove Colours</h1>
      <span class="picker__filename">{ name }</span>
    </div>

    <div class="picker__body">
      <div class="picker__viewer">
        <div class="picker__stage">
          <img class="picker__preview" id="preview" src="data:image/png;base64,{ preview }" alt="Preview" />
          <canvas class="picker__canvas" id="canvas" hidden></canvas>
        </div>

        <div class="picker__transport">
          <button class="picker__button picker__button--icon" id="play" type="button" disabled>Play</button>
          <button class="picker__button picker__button--icon" id="rewind" type="button" disabled>Rewind</button>
          <button class="picker__button picker__button--icon" id="loop" type="button">Loop</button>
          <input class="picker__scrub" id="scrub" type="range" min="0" max="0" value="0" disabled />
          <button class="picker__button picker__button--icon" id="animate" type="button">Build animation</button>
        </div>
      </div>

      <div class="picker__sidebar">
        <div class="picker__label">
          <span>Colours</span>
          <span id="count">0 selected</span>
        </div>

        <div class="picker__row">
          <select class="picker__select" id="sort">
            <option value="frequency">Frequency</option>
            <option value="hue">Hue</option>
            <option value="saturation">Saturation</option>
            <option value="luminance">Luminance</option>
          </select>

          <button class="picker__button picker__button--icon" id="direction" type="button">Desc</button>
        </div>

        <div class="picker__swatches" id="swatches"></div>

        <div class="picker__label">
          <span>Grouping threshold</span>
          <span id="threshold-value">{ DEFAULT_THRESHOLD }</span>
        </div>

        <input class="picker__slider" id="threshold" type="range" min="4" max="64" step="4" value="{ DEFAULT_THRESHOLD }" />

        <div class="picker__label">
          <span>Tolerance</span>
          <span id="tolerance-value">{ DEFAULT_TOLERANCE }%</span>
        </div>

        <input class="picker__slider" id="tolerance" type="range" min="0" max="40" value="{ DEFAULT_TOLERANCE }" />

        <div class="picker__label">
          <span>Action</span>
        </div>

        <div class="picker__row">
          <select class="picker__select" id="mode">
            <option value="transparent">Remove - make transparent</option>
            <option value="snap">Snap - flatten onto a colour</option>
          </select>

          <input class="picker__colour" id="target" type="color" value="#000000" title="Colour snapped pixels become" />
        </div>

        <div class="picker__label">
          <span>Frame disposal</span>
        </div>

        <select class="picker__select" id="disposal">
          <option value="Background">Background - clear each frame</option>
          <option value="None">None - leave each frame</option>
          <option value="Previous">Previous - restore prior frame</option>
          <option value="Auto">Auto - keep as authored</option>
        </select>

        <div class="picker__spacer"></div>

        <div class="picker__row">
          <button class="picker__button" id="reset" type="button">Reset</button>
          <button class="picker__button picker__button--primary" id="apply" type="button" disabled>Apply</button>
        </div>

        <div class="picker__status" id="status"></div>
      </div>
    </div>

    <script>
      const palette = { json.dumps(palette) };
      const basePreview = 'data:image/png;base64,{ preview }';
      const selected = new Set();

      const grid = document.getElementById('swatches');
      const preview = document.getElementById('preview');
      const canvas = document.getElementById('canvas');
      const context = canvas.getContext('2d');
      const status = document.getElementById('status');
      const count = document.getElementById('count');
      const applyButton = document.getElementById('apply');
      const sortSelect = document.getElementById('sort');
      const directionButton = document.getElementById('direction');
      const thresholdSlider = document.getElementById('threshold');
      const thresholdValue = document.getElementById('threshold-value');
      const toleranceSlider = document.getElementById('tolerance');
      const toleranceValue = document.getElementById('tolerance-value');
      const disposalSelect = document.getElementById('disposal');
      const modeSelect = document.getElementById('mode');
      const targetInput = document.getElementById('target');
      const animateButton = document.getElementById('animate');
      const playButton = document.getElementById('play');
      const rewindButton = document.getElementById('rewind');
      const loopButton = document.getElementById('loop');
      const scrub = document.getElementById('scrub');

      let timer = null;
      let closing = false;
      let descending = true;
      let anchor = null;
      let ordered = [];

      const player = {{ frames: [], delays: [], index: 0, playing: false, looping: true, raf: null, last: 0, elapsed: 0 }};

      /**
       * Converts a hex colour into its hue, saturation and luminance parts
       *
       * @param hex - Colour to convert
       */
      function toHsl(hex) {{
        const red = parseInt(hex.slice(1, 3), 16) / 255;
        const green = parseInt(hex.slice(3, 5), 16) / 255;
        const blue = parseInt(hex.slice(5, 7), 16) / 255;

        const highest = Math.max(red, green, blue);
        const lowest = Math.min(red, green, blue);
        const delta = highest - lowest;
        const luminance = (highest + lowest) / 2;

        let hue = 0;
        let saturation = 0;

        if (delta !== 0) {{
          saturation = luminance > 0.5 ? delta / (2 - highest - lowest) : delta / (highest + lowest);

          if (highest === red) {{
            hue = ((green - blue) / delta + (green < blue ? 6 : 0)) / 6;
          }}

          else if (highest === green) {{
            hue = ((blue - red) / delta + 2) / 6;
          }}

          else {{
            hue = ((red - green) / delta + 4) / 6;
          }}
        }}

        return {{ hue, saturation, luminance }};
      }}

      palette.forEach(entry => Object.assign(entry, toHsl(entry.hex)));

      /**
       * Collapses near-identical colours into representative swatches
       *
       * Every frame carries its own local colour table, so one visual colour
       * appears as dozens of near-identical entries. Grouping on a coarse RGB
       * grid folds those into a single swatch, and the threshold sets how
       * coarse that grid is
       *
       * @param size - Grid size per channel
       */
      function cluster(size) {{
        const groups = new Map();

        palette.forEach(entry => {{
          const red = parseInt(entry.hex.slice(1, 3), 16);
          const green = parseInt(entry.hex.slice(3, 5), 16);
          const blue = parseInt(entry.hex.slice(5, 7), 16);
          const key = `${{ Math.floor(red / size) }},${{ Math.floor(green / size) }},${{ Math.floor(blue / size) }}`;
          const existing = groups.get(key);

          if (!existing) {{
            groups.set(key, {{ ...entry, total: entry.share }});
          }}

          else {{
            existing.total += entry.share;

            if (entry.share > existing.share) {{
              Object.assign(existing, entry, {{ total: existing.total }});
            }}
          }}
        }});

        return [ ...groups.values() ];
      }}

      /**
       * Rebuilds the swatch grid from the current grouping and sort settings
       */
      function renderSwatches() {{
        const mode = sortSelect.value;
        const key = mode === 'frequency' ? 'total' : mode;

        ordered = cluster(Number(thresholdSlider.value))
          .sort((a, b) => descending ? b[ key ] - a[ key ] : a[ key ] - b[ key ]);

        grid.replaceChildren();

        ordered.forEach((entry, position) => {{
          const button = document.createElement('button');

          button.className = 'picker__swatch';
          button.style.background = entry.hex;
          button.title = `${{ entry.hex }} - ${{ entry.total.toFixed(2) }}% of pixels`;
          button.classList.toggle('picker__swatch--active', selected.has(entry.hex));

          button.addEventListener('click', event => toggle(entry.hex, position, event.shiftKey));
          grid.appendChild(button);
        }});
      }}

      /**
       * Adds or removes colours from the selection
       *
       * Shift extends from the last plain click across the visible order, so
       * a run of similar colours can be taken in one go after sorting by hue
       *
       * @param hex - Colour the swatch represents
       * @param position - Index of the swatch in the current order
       * @param extend - Whether shift was held
       */
      function toggle(hex, position, extend) {{
        if (extend && anchor !== null) {{
          const from = Math.min(anchor, position);
          const to = Math.max(anchor, position);

          for (let step = from; step <= to; step += 1) {{
            selected.add(ordered[ step ].hex);
          }}
        }}

        else {{
          if (selected.has(hex)) {{
            selected.delete(hex);
          }}

          else {{
            selected.add(hex);
          }}

          anchor = position;
        }}

        renderSwatches();
        refresh();
      }}

      /**
       * Updates the counters and schedules a debounced single-frame preview
       */
      function refresh() {{
        count.textContent = `${{ selected.size }} selected`;
        applyButton.disabled = selected.size === 0;

        clearTimeout(timer);
        timer = setTimeout(renderPreview, 120);
      }}

      /**
       * Returns the current settings as sent to the server
       */
      function settings() {{
        return {{
          colours: [ ...selected ],
          tolerance: Number(toleranceSlider.value),
          disposal: disposalSelect.value,
          mode: modeSelect.value,
          target: targetInput.value
        }};
      }}

      /**
       * Asks the server for a freshly rendered preview frame
       */
      async function renderPreview() {{
        stop();
        canvas.hidden = true;
        preview.hidden = false;

        if (selected.size === 0) {{
          preview.src = basePreview;
          status.textContent = '';

          return;
        }}

        const response = await fetch('/preview', {{ method: 'POST', body: JSON.stringify(settings()) }});

        preview.src = URL.createObjectURL(await response.blob());
        status.textContent = '';
      }}

      /**
       * Draws the frame at the player's current index
       */
      function draw() {{
        const frame = player.frames[ player.index ];

        if (frame) {{
          context.clearRect(0, 0, canvas.width, canvas.height);
          context.drawImage(frame, 0, 0);
          scrub.value = String(player.index);
        }}
      }}

      /**
       * Advances playback in step with each frame's authored delay
       *
       * @param stamp - Timestamp handed over by requestAnimationFrame
       */
      function tick(stamp) {{
        if (!player.playing) {{
          return;
        }}

        if (!player.last) {{
          player.last = stamp;
        }}

        player.elapsed += stamp - player.last;
        player.last = stamp;

        const delay = player.delays[ player.index ] || 100;

        if (player.elapsed >= delay) {{
          player.elapsed -= delay;
          player.index += 1;

          if (player.index >= player.frames.length) {{
            if (!player.looping) {{
              player.index = player.frames.length - 1;
              stop();
              draw();

              return;
            }}

            player.index = 0;
          }}

          draw();
        }}

        player.raf = requestAnimationFrame(tick);
      }}

      /**
       * Starts playback from the current position
       */
      function play() {{
        if (!player.frames.length) {{
          return;
        }}

        player.playing = true;
        player.last = 0;
        playButton.textContent = 'Pause';
        player.raf = requestAnimationFrame(tick);
      }}

      /**
       * Halts playback without moving the playhead
       */
      function stop() {{
        player.playing = false;
        playButton.textContent = 'Play';

        if (player.raf) {{
          cancelAnimationFrame(player.raf);
          player.raf = null;
        }}
      }}

      animateButton.addEventListener('click', async () => {{
        animateButton.disabled = true;
        status.textContent = 'Building animation, longer clips take a moment';

        const response = await fetch('/animate', {{ method: 'POST', body: JSON.stringify(settings()) }});
        const result = await response.json();

        animateButton.disabled = false;

        if (!result.ok) {{
          status.textContent = result.message;

          return;
        }}

        player.delays = result.delays;
        player.frames = await Promise.all(result.frames.map(data => new Promise(resolve => {{
          const image = new Image();

          image.onload = () => resolve(image);
          image.src = `data:image/png;base64,${{ data }}`;
        }})));

        canvas.width = player.frames[ 0 ].width;
        canvas.height = player.frames[ 0 ].height;
        canvas.hidden = false;
        preview.hidden = true;

        player.index = 0;
        player.elapsed = 0;
        scrub.max = String(player.frames.length - 1);
        scrub.disabled = false;
        playButton.disabled = false;
        rewindButton.disabled = false;

        status.textContent = `${{ player.frames.length }} frames, ${{ {ANIMATION_LOOPS} }} loops - watch the loop point for stacking`;

        draw();
        play();
      }});

      playButton.addEventListener('click', () => player.playing ? stop() : play());

      rewindButton.addEventListener('click', () => {{
        player.index = 0;
        player.elapsed = 0;
        draw();
      }});

      loopButton.addEventListener('click', () => {{
        player.looping = !player.looping;
        loopButton.classList.toggle('picker__button--on', player.looping);
      }});

      scrub.addEventListener('input', () => {{
        stop();
        player.index = Number(scrub.value);
        draw();
      }});

      directionButton.addEventListener('click', () => {{
        descending = !descending;
        directionButton.textContent = descending ? 'Desc' : 'Asc';
        renderSwatches();
      }});

      sortSelect.addEventListener('change', renderSwatches);

      thresholdSlider.addEventListener('input', () => {{
        thresholdValue.textContent = thresholdSlider.value;
        anchor = null;
        renderSwatches();
      }});

      toleranceSlider.addEventListener('input', () => {{
        toleranceValue.textContent = `${{ toleranceSlider.value }}%`;
        refresh();
      }});

      document.getElementById('reset').addEventListener('click', () => {{
        selected.clear();
        anchor = null;
        renderSwatches();
        refresh();
      }});

      applyButton.addEventListener('click', async () => {{
        applyButton.disabled = true;
        status.textContent = 'Processing every frame, this takes a moment';

        const response = await fetch('/apply', {{ method: 'POST', body: JSON.stringify(settings()) }});
        const result = await response.json();

        status.textContent = result.message;

        if (result.ok) {{
          closing = true;
          setTimeout(() => window.close(), 1_200);
        }}
      }});

      // The server exits when these stop arriving, so closing the window
      // never leaves the Quick Action hanging
      setInterval(() => fetch('/ping', {{ method: 'POST' }}).catch(() => {{}}), { HEARTBEAT_INTERVAL * 1_000 });

      window.addEventListener('pagehide', () => {{
        if (!closing) {{
          navigator.sendBeacon('/cancel');
        }}
      }});

      modeSelect.addEventListener('change', () => {{
        targetInput.disabled = modeSelect.value !== 'snap';
        refresh();
      }});

      targetInput.addEventListener('input', refresh);

      // Snapping almost always targets the background, which is the colour
      // covering the most pixels
      if (palette.length) {{
        targetInput.value = palette[ 0 ].hex.toLowerCase();
      }}

      targetInput.disabled = true;
      loopButton.classList.add('picker__button--on');
      renderSwatches();
    </script>
  </body>
</html>"""


def serve(source, destination, palette, preview, frame = 0):
    """
    Runs the picker GUI and blocks until the user applies or closes it

    @param source - Source GIF being edited
    @param destination - Path the processed GIF is written to
    @param palette - Sampled colours with their pixel share
    @param preview - Base64 PNG of the unmodified preview frame
    @param frame - Index of the frame shown in the still preview
    """

    page = build_page(source.name, palette, preview).encode()
    finished = threading.Event()
    outcome = {}
    heartbeat = { 'seen': None }

    class Handler(BaseHTTPRequestHandler):
        """
        Serves the picker page and the preview, animate and apply endpoints
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
            Serves the picker page
            """

            if self.path == '/':
                self.respond(200, page, 'text/html; charset=utf-8')

                return

            self.respond(404, b'', 'text/plain')

        def do_POST(self):
            """
            Handles heartbeats, previews, animation builds and the final apply
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
            colours = payload.get('colours', [])
            tolerance = payload.get('tolerance', DEFAULT_TOLERANCE)
            disposal = payload.get('disposal', DEFAULT_DISPOSAL)
            mode = payload.get('mode', 'transparent')
            target = payload.get('target', '#000000')

            if self.path == '/preview':
                self.respond(200, render_preview(source, colours, tolerance, frame, mode, target), 'image/png')

                return

            if self.path == '/animate':
                try:
                    result = render_animation(source, colours, tolerance, disposal, mode, target)
                    result[ 'ok' ] = True

                except (RuntimeError, subprocess.CalledProcessError) as error:
                    result = { 'ok': False, 'message': str(error) }

                self.respond(200, json.dumps(result).encode(), 'application/json')

                return

            if self.path == '/apply':
                try:
                    apply_removal(source, destination, colours, tolerance, disposal, mode, target)
                    size = destination.stat().st_size // 1_024
                    outcome.update({ 'ok': True, 'message': f'Saved { destination.name } ({ size }KB)' })

                except RuntimeError as error:
                    outcome.update({ 'ok': False, 'message': str(error) })

                self.respond(200, json.dumps(outcome).encode(), 'application/json')
                finished.set()

                return

            self.respond(404, b'', 'text/plain')

    with socket.socket() as probe:
        probe.bind(( '127.0.0.1', 0 ))
        port = probe.getsockname()[ 1 ]

    server = ThreadingHTTPServer(( '127.0.0.1', port ), Handler)
    thread = threading.Thread(target = server.serve_forever, daemon = True)

    thread.start()

    def watchdog():
        """
        Ends the session once the page stops sending heartbeats

        The beacon fired on window close is best-effort and never arrives if
        the browser is force quit, so a missed-heartbeat timeout is what
        actually guarantees the script exits instead of blocking forever
        """

        started = time.monotonic()

        while not finished.wait(HEARTBEAT_INTERVAL):
            seen = heartbeat[ 'seen' ]

            # Nothing has loaded the page yet, so the browser itself may have failed
            if seen is None:
                if time.monotonic() - started > STARTUP_GRACE:
                    outcome.update({ 'ok': False, 'message': 'Browser window never opened, giving up' })
                    finished.set()

                continue

            if time.monotonic() - seen > HEARTBEAT_GRACE:
                finished.set()

    url = f'http://127.0.0.1:{ port }/'
    binary = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')

    threading.Thread(target = watchdog, daemon = True).start()

    if binary.exists():
        # A dedicated profile keeps the window independent of any running Chrome
        profile = Path.home() / 'Library/Caches/remove-colours-chrome'

        subprocess.Popen(
            [ str(binary), f'--app={ url }', f'--user-data-dir={ profile }', f'--window-size={ PREVIEW_WIDTH + 420 },820', '--no-first-run', '--no-default-browser-check' ],
            stdout = subprocess.DEVNULL, stderr = subprocess.DEVNULL
        )

    else:
        webbrowser.open(url)

    print(f'Picker open at { url } - close the window to cancel', flush = True)

    try:
        finished.wait()

    except KeyboardInterrupt:
        pass

    server.shutdown()

    return outcome


def process(path, tolerance, colours, disposal, mode = 'transparent', target = '#000000'):
    """
    Runs one file through the pipeline, with or without the GUI

    @param path - Input file, either a GIF or a movie
    @param tolerance - Fuzzy match percentage
    @param colours - Colours to strip without opening the GUI
    @param disposal - Frame disposal mode to write
    @param mode - Either transparent or snap
    @param target - Colour that snapped pixels become
    """

    source = Path(path).resolve()

    if not source.exists():
        print(f"Warning: '{ source }' not found, skipping")

        return

    if source.suffix.lower() in MOVIE_SUFFIXES:
        print(f'Converting { source.name } with Photoshop')
        source = convert_movie(source)

    destination = source.with_name(f'{ source.stem }-nocolour.gif')

    if colours:
        apply_removal(source, destination, colours, tolerance, disposal, mode, target)
        print(f'Saved { destination.name } ({ destination.stat().st_size // 1_024 }KB)')

        return

    print(f'Sampling palette from { source.name }')

    palette = sample_palette(source)
    frame = representative_frame(source)
    preview = base64.b64encode(render_preview(source, [], tolerance, frame)).decode()
    outcome = serve(source, destination, palette, preview, frame)

    if outcome:
        print(outcome[ 'message' ])

    else:
        print('Cancelled')


def main():
    """
    Parses arguments and runs each input file through the pipeline
    """

    parser = ArgumentParser(description = 'Remove colours from animated GIFs')

    parser.add_argument('files', nargs = '+', help = 'GIF or movie files to process')
    parser.add_argument('-t', '--tolerance', type = int, default = DEFAULT_TOLERANCE, help = 'fuzzy match percentage')
    parser.add_argument('-x', '--remove', action = 'append', default = [], metavar = 'HEX', help = 'strip a colour without opening the GUI')
    parser.add_argument('-d', '--disposal', default = DEFAULT_DISPOSAL, choices = [ 'Background', 'None', 'Previous', 'Auto' ], help = 'frame disposal mode')
    parser.add_argument('-m', '--mode', default = 'transparent', choices = [ 'transparent', 'snap' ], help = 'cut colours out, or flatten them onto --target')
    parser.add_argument('--target', default = '#000000', metavar = 'HEX', help = 'colour that snapped pixels become')
    parser.add_argument('--alert', action = 'store_true', help = 'report failures as a native alert rather than on stderr')

    arguments = parser.parse_args()

    for path in arguments.files:
        try:
            process(path, arguments.tolerance, arguments.remove, arguments.disposal, arguments.mode, arguments.target)

        except (Exception, SystemExit) as error:
            detail = str(error) or error.__class__.__name__

            print(f'Error on { Path(path).name }: { detail }', file = sys.stderr, flush = True)

            if arguments.alert:
                notify('Remove Colours', f'{ Path(path).name }\\n\\n{ detail }'.replace('"', "'"))


if __name__ == '__main__':
    main()
