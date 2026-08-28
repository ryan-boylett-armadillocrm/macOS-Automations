#!/usr/bin/env python3

"""
Dial in Photoshop's movie to GIF conversion through a local browser UI

Pulls one representative frame out of the movie, round-trips it through
Photoshop's Save for Web encoder at the current settings, and shows it
against the untouched source so quantisation artefacts can be judged
before committing to a full render

Once the settings look right the whole movie goes through mov-to-gif.sh
with those exact values

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

PREVIEW_WIDTH = 650
DEFAULT_COLOURS = 256
DEFAULT_DITHER = 100
DEFAULT_REDUCTION = 'adaptive'
DEFAULT_WIDTH = 650
REDUCTIONS = ( 'selective', 'perceptual', 'adaptive', 'web' )
HEARTBEAT_INTERVAL = 2
HEARTBEAT_GRACE = 8
STARTUP_GRACE = 45

CONVERTER_CANDIDATES = (
    Path(__file__).resolve().parent / 'mov-to-gif.sh',
    Path.home() / 'Library/Services/Remove Colours.workflow/Contents/Resources/mov-to-gif.sh',
    Path.home() / 'Library/CloudStorage/OneDrive-BondBrandLoyalty,Inc/Scripts/mov-to-gif.sh',
)


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


def find_converter():
    """
    Locates a readable copy of mov-to-gif.sh

    Readability matters more than existence, because macOS denies
    Finder-invoked services access to CloudStorage even when the file is there
    """

    for candidate in CONVERTER_CANDIDATES:
        try:
            candidate.open('rb').close()

            return candidate

        except OSError:
            continue

    locations = ' or '.join(str(candidate) for candidate in CONVERTER_CANDIDATES)

    raise RuntimeError(f'mov-to-gif.sh is not readable at { locations }')


def photoshop_name():
    """
    Returns the newest installed Photoshop's application name
    """

    apps = sorted(Path('/Applications').glob('Adobe Photoshop */Adobe Photoshop *.app'), reverse = True)

    if not apps:
        raise RuntimeError('Adobe Photoshop not found in /Applications')

    return apps[ 0 ].stem


def movie_frames(path):
    """
    Counts the frames in a movie so a representative one can be chosen

    @param path - Movie to inspect
    """

    result = subprocess.run(
        [ require_binary('ffprobe'), '-v', 'error', '-select_streams', 'v:0',
          '-count_frames', '-show_entries', 'stream=nb_read_frames', '-of', 'csv=p=0', str(path) ],
        capture_output = True, text = True
    )

    match = re.search(r'\d+', result.stdout)

    return int(match.group()) if match else 1


def extract_frame(path, index, width, destination):
    """
    Pulls a single frame out of the movie as a PNG

    Passing no width keeps the frame at its native size. That matters for
    anything headed to Photoshop, because mov-to-gif.sh resizes inside
    Photoshop with bicubic, and pre-scaling here with a different resampler
    shifts the colours of fine detail enough to misrepresent the result

    @param path - Movie to read
    @param index - Frame number to extract
    @param width - Width to scale to, or None to keep native size
    @param destination - Path the PNG is written to
    """

    scale = f',scale={ width }:-1' if width else ''

    subprocess.run(
        [ require_binary('ffmpeg'), '-v', 'error', '-i', str(path),
          '-vf', f'select=eq(n\\,{ index }){ scale }', '-frames:v', '1', '-y', str(destination) ],
        capture_output = True, check = True
    )

    return destination


def photoshop_script(body):
    """
    Runs an ExtendScript snippet in Photoshop and returns its stderr

    @param body - ExtendScript source to execute
    """

    name = photoshop_name()

    with tempfile.NamedTemporaryFile('w', suffix = '.jsx', delete = False) as handle:
        handle.write(body)
        script = handle.name

    result = subprocess.run(
        [ 'osascript', '-e', f'tell application "{ name }" to do javascript file "{ script }"' ],
        capture_output = True, text = True
    )

    Path(script).unlink(missing_ok = True)

    return result.stderr.strip()


def photoshop_resize(source, destination, width):
    """
    Resizes a frame through Photoshop so the reference view matches the encode

    Both views have to come off the same resampler, otherwise the comparison
    shows ImageMagick's interpolation rather than what Photoshop will produce

    @param source - PNG frame at native size
    @param destination - PNG path to write
    @param width - Target width in pixels
    """

    error = photoshop_script(f"""(function () {{
  app.displayDialogs = DialogModes.NO;

  var doc = app.open(new File("{ source }"));
  var targetWidth = { width };
  var targetHeight = Math.round(targetWidth * (doc.height.as("px") / doc.width.as("px")));

  doc.resizeImage(UnitValue(targetWidth, "px"), UnitValue(targetHeight, "px"), doc.resolution, ResampleMethod.BICUBIC);

  var options = new PNGSaveOptions();

  doc.saveAs(new File("{ destination }"), options, true, Extension.LOWERCASE);
  doc.close(SaveOptions.DONOTSAVECHANGES);
}})();""")

    if not Path(destination).exists():
        raise RuntimeError(error or 'Photoshop could not resize the frame')

    return destination


def photoshop_encode(source, destination, settings):
    """
    Round-trips one frame through Photoshop's Save for Web GIF encoder

    This is the same encoder the full conversion uses, so what the preview
    shows is what the finished GIF will contain. ImageMagick's quantiser
    would be faster but would not match Photoshop's output

    @param source - PNG frame to encode
    @param destination - GIF path to write
    @param settings - Colour, dither and reduction values from the UI
    """

    name = photoshop_name()
    reduction = settings.get('reduction', DEFAULT_REDUCTION)
    transparency = 'true' if settings.get('transparency') else 'false'

    with tempfile.NamedTemporaryFile('w', suffix = '.jsx', delete = False) as handle:
        handle.write(f"""(function () {{
  app.displayDialogs = DialogModes.NO;

  var doc = app.open(new File("{ source }"));

  // Resize here rather than upstream so the preview goes through the same
  // bicubic step mov-to-gif.sh uses on the real render
  var targetWidth = { settings.get('width', DEFAULT_WIDTH) };
  var targetHeight = Math.round(targetWidth * (doc.height.as("px") / doc.width.as("px")));

  doc.resizeImage(UnitValue(targetWidth, "px"), UnitValue(targetHeight, "px"), doc.resolution, ResampleMethod.BICUBIC);

  var opts = new ExportOptionsSaveForWeb();

  opts.format = SaveDocumentType.COMPUSERVEGIF;
  opts.colors = { settings.get('colours', DEFAULT_COLOURS) };
  opts.dither = Dither.DIFFUSION;
  opts.ditherAmount = { settings.get('dither', DEFAULT_DITHER) };
  opts.transparency = { transparency };
  opts.interlaced = false;

  // ColorReduction was removed in Photoshop 2026, so guard against its absence
  if (typeof ColorReduction !== 'undefined') {{
    var map = {{
      selective: ColorReduction.SELECTIVE,
      perceptual: ColorReduction.PERCEPTUAL,
      adaptive: ColorReduction.ADAPTIVE,
      web: ColorReduction.WEB
    }};

    opts.colorReduction = map["{ reduction }"] || ColorReduction.SELECTIVE;
  }}

  doc.exportDocument(new File("{ destination }"), ExportType.SAVEFORWEB, opts);
  doc.close(SaveOptions.DONOTSAVECHANGES);
}})();""")
        script = handle.name

    result = subprocess.run(
        [ 'osascript', '-e', f'tell application "{ name }" to do javascript file "{ script }"' ],
        capture_output = True, text = True
    )

    Path(script).unlink(missing_ok = True)

    if not Path(destination).exists():
        raise RuntimeError(result.stderr.strip() or 'Photoshop produced no output')

    return destination


def convert_movie(path, settings):
    """
    Runs the full movie through mov-to-gif.sh at the chosen settings

    @param path - Movie to convert
    @param settings - Colour, dither, reduction and width values from the UI
    """

    command = [
        'bash', str(find_converter()),
        '-c', str(settings.get('colours', DEFAULT_COLOURS)),
        '-d', str(settings.get('dither', DEFAULT_DITHER)),
        '-r', settings.get('reduction', DEFAULT_REDUCTION),
        '-w', str(settings.get('width', DEFAULT_WIDTH)),
    ]

    if settings.get('transparency'):
        command.append('-T')

    command.append(str(path))

    result = subprocess.run(command, capture_output = True, text = True)
    produced = path.with_suffix('.gif')

    if result.returncode != 0 or not produced.exists():
        raise RuntimeError(result.stderr.strip() or 'Photoshop did not produce a GIF')

    return produced


def build_page(name, source, total = 1, current = 0):
    """
    Renders the settings UI as a single self-contained page

    @param name - Filename shown in the header
    @param source - Base64 PNG of the untouched reference frame
    @param total - Frame count of the movie
    @param current - Frame the reference image was taken from
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
        gap: 14px;
        height: 100vh;
        margin: 0;
        padding: 16px;

        font-family: -apple-system, BlinkMacSystemFont, 'Helvetica Neue', sans-serif;
        font-size: 13px;
        color: var(--text);

        background: var(--bg);
      }}

      .settings__header {{
        display: flex;
        align-items: baseline;
        gap: 8px;
      }}

      .settings__title {{
        margin: 0;

        font-size: 14px;
        font-weight: 600;
      }}

      .settings__filename {{
        color: var(--muted);
      }}

      .settings__body {{
        display: grid;
        grid-template-columns: 1fr 300px;
        gap: 16px;
        flex: 1;
        min-height: 0;
      }}

      .settings__viewer {{
        display: flex;
        flex-direction: column;
        gap: 10px;
        min-width: 0;
      }}

      .settings__stage {{
        display: flex;
        align-items: center;
        justify-content: center;
        flex: 1;
        min-height: 0;
        overflow: auto;
        padding: 12px;

        border: 1px solid var(--border);
        border-radius: 6px;
        background: #2a2a2a;
      }}

      .settings__image {{
        image-rendering: pixelated;
      }}

      .settings__transport {{
        display: flex;
        align-items: center;
        gap: 8px;
      }}

      .settings__sidebar {{
        display: flex;
        flex-direction: column;
        gap: 10px;
        min-height: 0;
      }}

      .settings__label {{
        display: flex;
        justify-content: space-between;

        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        color: var(--muted);
      }}

      .settings__row {{
        display: flex;
        gap: 6px;
      }}

      .settings__select {{
        width: 100%;
        padding: 5px 8px;

        font-family: inherit;
        font-size: 12px;
        color: var(--text);

        border: 1px solid var(--border);
        border-radius: 6px;
        background: var(--panel);
      }}

      .settings__slider {{
        width: 100%;

        accent-color: var(--accent);
      }}

      .settings__button {{
        flex: 1;
        padding: 7px 12px;

        font-size: 13px;
        color: var(--text);

        border: 1px solid var(--border);
        border-radius: 6px;
        background: #333333;
        cursor: pointer;
      }}

      .settings__button--icon {{
        flex: 0 0 auto;
        min-width: 44px;
        padding: 5px 10px;
      }}

      .settings__button--primary {{
        color: #ffffff;

        border-color: transparent;
        background: var(--accent);
      }}

      .settings__button--on {{
        color: #ffffff;

        border-color: var(--accent);
      }}

      .settings__button:disabled {{
        opacity: 0.4;
        cursor: default;
      }}

      .settings__spacer {{
        flex: 1 1 0;
        min-height: 8px;
      }}

      .settings__status {{
        min-height: 15px;

        font-size: 11px;
        color: var(--muted);
      }}
    </style>
  </head>

  <body>
    <div class="settings__header">
      <h1 class="settings__title">Convert to GIF</h1>
      <span class="settings__filename">{ name }</span>
    </div>

    <div class="settings__body">
      <div class="settings__viewer">
        <div class="settings__stage">
          <img class="settings__image" id="image" src="data:image/png;base64,{ source }" alt="Preview" />
        </div>

        <div class="settings__transport">
          <button class="settings__button settings__button--icon" id="toggle" type="button">Source</button>
          <button class="settings__button settings__button--icon" id="zoom" type="button">100%</button>
          <span class="settings__status" id="showing">showing source</span>
          <button class="settings__button settings__button--icon" id="render" type="button">Preview</button>
        </div>
      </div>

      <div class="settings__sidebar">
        <div class="settings__label">
          <span>Colours</span>
          <span id="colours-value">{ DEFAULT_COLOURS }</span>
        </div>

        <input class="settings__slider" id="colours" type="range" min="2" max="256" step="2" value="{ DEFAULT_COLOURS }" />

        <div class="settings__label">
          <span>Dither</span>
          <span id="dither-value">{ DEFAULT_DITHER }%</span>
        </div>

        <input class="settings__slider" id="dither" type="range" min="0" max="100" value="{ DEFAULT_DITHER }" />

        <div class="settings__label">
          <span>Colour reduction</span>
        </div>

        <select class="settings__select" id="reduction">
          <option value="adaptive">Adaptive</option>
          <option value="selective">Selective</option>
          <option value="perceptual">Perceptual</option>
          <option value="web">Web</option>
        </select>

        <div class="settings__label">
          <span>Output width</span>
          <span id="width-value">{ DEFAULT_WIDTH }px</span>
        </div>

        <input class="settings__slider" id="width" type="range" min="120" max="1200" step="10" value="{ DEFAULT_WIDTH }" />

        <div class="settings__label">
          <span>Frame</span>
          <span id="frame-value">{ current } / { max(total - 1, 0) }</span>
        </div>

        <input class="settings__slider" id="frame" type="range" min="0" max="{ max(total - 1, 0) }" value="{ current }" />

        <div class="settings__spacer"></div>

        <div class="settings__row">
          <button class="settings__button" id="reset" type="button">Reset</button>
          <button class="settings__button settings__button--primary" id="convert" type="button">Convert</button>
        </div>

        <div class="settings__status" id="status"></div>
      </div>
    </div>

    <script>
      const sourceImage = 'data:image/png;base64,{ source }';

      const image = document.getElementById('image');
      const toggleButton = document.getElementById('toggle');
      const zoomButton = document.getElementById('zoom');
      const showing = document.getElementById('showing');
      const renderButton = document.getElementById('render');
      const convertButton = document.getElementById('convert');
      const status = document.getElementById('status');
      const frameSlider = document.getElementById('frame');
      const frameValue = document.getElementById('frame-value');

      const controls = [
        [ 'colours', 'colours-value', value => value ],
        [ 'dither', 'dither-value', value => `${{ value }}%` ],
        [ 'width', 'width-value', value => `${{ value }}px` ]
      ];

      let rendered = null;
      let viewing = 'source';
      let zoom = 1;
      let busy = false;

      /**
       * Returns the current settings as sent to the server
       */
      function settings() {{
        return {{
          colours: Number(document.getElementById('colours').value),
          dither: Number(document.getElementById('dither').value),
          reduction: document.getElementById('reduction').value,
          width: Number(document.getElementById('width').value),
          frame: Number(frameSlider.value)
        }};
      }}

      /**
       * Shows either the untouched source frame or the encoded result
       *
       * @param which - Either source or rendered
       */
      function show(which) {{
        viewing = which;
        image.src = which === 'rendered' && rendered ? rendered : sourceImage;
        showing.textContent = which === 'rendered' ? 'showing encoded' : 'showing source';
        toggleButton.textContent = which === 'rendered' ? 'Encoded' : 'Source';
        toggleButton.classList.toggle('settings__button--on', which === 'rendered');
      }}

      /**
       * Asks Photoshop to encode the current frame at the current settings
       */
      async function render() {{
        if (busy) {{
          return;
        }}

        busy = true;
        renderButton.disabled = true;
        status.textContent = 'Encoding in Photoshop';

        try {{
          const response = await fetch('/preview', {{ method: 'POST', body: JSON.stringify(settings()) }});
          const result = await response.json();

          if (!result.ok) {{
            status.textContent = result.message;

            return;
          }}

          rendered = `data:image/gif;base64,${{ result.image }}`;
          status.textContent = `${{ result.colours }} colours in this frame`;
          show('rendered');
        }}

        finally {{
          busy = false;
          renderButton.disabled = false;
        }}
      }}

      /**
       * Pulls the selected frame again and shows it as the reference view
       */
      async function restage() {{
        status.textContent = 'Extracting frame';

        const response = await fetch('/frame', {{ method: 'POST', body: JSON.stringify(settings()) }});
        const result = await response.json();

        status.textContent = result.ok ? '' : result.message;

        if (result.ok) {{
          image.src = `data:image/png;base64,${{ result.image }}`;
          rendered = null;
          show('source');
        }}
      }}

      controls.forEach(([ id, output, format ]) => {{
        const input = document.getElementById(id);
        const label = document.getElementById(output);

        input.addEventListener('input', () => {{ label.textContent = format(input.value); }});

        // Width changes the reference view too, since Photoshop resizes both
        input.addEventListener('change', async () => {{
          if (id === 'width') {{
            await restage();
          }}

          render();
        }});
      }});

      document.getElementById('reduction').addEventListener('change', render);

      frameSlider.addEventListener('input', () => {{ frameValue.textContent = `${{ frameSlider.value }} / ${{ frameSlider.max }}`; }});
      frameSlider.addEventListener('change', restage);

      toggleButton.addEventListener('click', () => show(viewing === 'source' ? 'rendered' : 'source'));

      zoomButton.addEventListener('click', () => {{
        zoom = zoom === 1 ? 2 : zoom === 2 ? 4 : 1;
        zoomButton.textContent = `${{ zoom * 100 }}%`;
        image.style.width = zoom === 1 ? 'auto' : `${{ image.naturalWidth * zoom }}px`;
      }});

      renderButton.addEventListener('click', render);

      document.getElementById('reset').addEventListener('click', () => {{
        document.getElementById('colours').value = {DEFAULT_COLOURS};
        document.getElementById('dither').value = {DEFAULT_DITHER};
        document.getElementById('width').value = {DEFAULT_WIDTH};
        document.getElementById('reduction').value = '{DEFAULT_REDUCTION}';
        document.getElementById('colours-value').textContent = '{DEFAULT_COLOURS}';
        document.getElementById('dither-value').textContent = '{DEFAULT_DITHER}%';
        document.getElementById('width-value').textContent = '{DEFAULT_WIDTH}px';
        render();
      }});

      convertButton.addEventListener('click', async () => {{
        convertButton.disabled = true;
        status.textContent = 'Converting the whole movie, this takes a while';

        const response = await fetch('/convert', {{ method: 'POST', body: JSON.stringify(settings()) }});
        const result = await response.json();

        status.textContent = result.message;

        if (result.ok) {{
          window.closing = true;
          setTimeout(() => window.close(), 1_500);
        }}

        else {{
          convertButton.disabled = false;
        }}
      }});

      setInterval(() => fetch('/ping', {{ method: 'POST' }}).catch(() => {{}}), { HEARTBEAT_INTERVAL * 1_000 });

      window.addEventListener('pagehide', () => {{
        if (!window.closing) {{
          navigator.sendBeacon('/cancel');
        }}
      }});
    </script>
  </body>
</html>"""


def serve(source, work, total):
    """
    Runs the settings UI and blocks until the user converts or closes it

    @param source - Movie being converted
    @param work - Directory used for preview frames
    @param total - Frame count of the movie
    """

    middle = max(0, total // 2)

    def stage_frame(index, width = DEFAULT_WIDTH):
        """
        Extracts one frame at native size and returns a Photoshop-resized copy

        @param index - Frame number to pull from the movie
        @param width - Width the reference view is resized to
        """

        extract_frame(source, index, None, work / 'frame.png')

        display = work / 'display.png'

        display.unlink(missing_ok = True)
        photoshop_resize(work / 'frame.png', display, width)

        return display.read_bytes()

    page = build_page(source.name, base64.b64encode(stage_frame(middle)).decode(), total, middle).encode()

    finished = threading.Event()
    outcome = {}
    heartbeat = { 'seen': None }

    class Handler(BaseHTTPRequestHandler):
        """
        Serves the settings page, frame previews and the final conversion
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
            Serves the settings page
            """

            if self.path == '/':
                self.respond(200, page, 'text/html; charset=utf-8')

                return

            self.respond(404, b'', 'text/plain')

        def do_POST(self):
            """
            Handles heartbeats, frame changes, previews and the conversion
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

            if self.path == '/frame':
                try:
                    body = {
                        'ok': True,
                        'image': base64.b64encode(
                            stage_frame(payload.get('frame', middle), payload.get('width', DEFAULT_WIDTH))
                        ).decode()
                    }

                except (RuntimeError, subprocess.CalledProcessError) as error:
                    body = { 'ok': False, 'message': str(error) }

                self.respond(200, json.dumps(body).encode(), 'application/json')

                return

            if self.path == '/preview':
                try:
                    encoded = work / 'preview.gif'
                    encoded.unlink(missing_ok = True)

                    photoshop_encode(work / 'frame.png', encoded, payload)
                    colours = subprocess.run(
                        [ require_binary('magick'), str(encoded), '-format', '%k', 'info:' ],
                        capture_output = True, text = True
                    ).stdout.strip()

                    body = {
                        'ok': True,
                        'image': base64.b64encode(encoded.read_bytes()).decode(),
                        'colours': colours or '?'
                    }

                except (RuntimeError, subprocess.CalledProcessError) as error:
                    body = { 'ok': False, 'message': str(error) }

                self.respond(200, json.dumps(body).encode(), 'application/json')

                return

            if self.path == '/convert':
                try:
                    produced = convert_movie(source, payload)
                    size = produced.stat().st_size // 1_024
                    outcome.update({ 'ok': True, 'message': f'Saved { produced.name } ({ size }KB)' })

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

            # A full conversion blocks the request thread well past the grace
            # period, so only idle sessions are timed out
            if time.monotonic() - seen > HEARTBEAT_GRACE and not outcome:
                finished.set()

    url = f'http://127.0.0.1:{ port }/'
    binary = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')

    threading.Thread(target = watchdog, daemon = True).start()

    if binary.exists():
        profile = Path.home() / 'Library/Caches/convert-to-gif-chrome'

        subprocess.Popen(
            [ str(binary), f'--app={ url }', f'--user-data-dir={ profile }', '--window-size=1080,820', '--no-first-run', '--no-default-browser-check' ],
            stdout = subprocess.DEVNULL, stderr = subprocess.DEVNULL
        )

    else:
        webbrowser.open(url)

    print(f'Settings open at { url } - close the window to cancel', flush = True)

    try:
        finished.wait()

    except KeyboardInterrupt:
        pass

    server.shutdown()

    return outcome


def process(path):
    """
    Opens the settings UI for one movie

    @param path - Movie to convert
    """

    source = Path(path).resolve()

    if not source.exists():
        print(f"Warning: '{ source }' not found, skipping")

        return

    total = movie_frames(source)

    print(f'Reading { source.name } ({ total } frames)')

    with tempfile.TemporaryDirectory() as work:
        outcome = serve(source, Path(work), total)

    if outcome:
        print(outcome[ 'message' ])

    else:
        print('Cancelled')


def main():
    """
    Parses arguments and opens the settings UI for each movie
    """

    parser = ArgumentParser(description = 'Dial in Photoshop movie to GIF conversion')

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
