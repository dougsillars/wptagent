#!/usr/bin/env python
# Copyright 2017 Google Inc. All rights reserved.
# Use of this source code is governed by the Apache 2.0 license that can be
# found in the LICENSE file.
"""WebPageTest cross-platform agent"""
from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
import atexit
import logging
import logging.handlers
import os
import platform
import Queue
import re
import signal
import subprocess
import sys
import threading
import time
import traceback

class WPTAgent(object):
    """Main agent workflow"""
    def __init__(self, options, browsers):
        from internal.browsers import Browsers
        from internal.webpagetest import WebPageTest
        from internal.traffic_shaping import TrafficShaper
        from internal.adb import Adb
        from internal.ios_device import iOSDevice
        self.must_exit = False
        self.options = options
        self.capture_display = None
        self.job = None
        self.task = None
        self.xvfb = None
        self.root_path = os.path.abspath(os.path.dirname(__file__))
        self.wpt = WebPageTest(options, os.path.join(self.root_path, "work"))
        self.persistent_work_dir = self.wpt.get_persistent_dir()
        self.adb = Adb(self.options, self.persistent_work_dir) if self.options.android else None
        self.ios = iOSDevice(self.options.device) if self.options.iOS else None
        self.browsers = Browsers(options, browsers, self.adb, self.ios)
        self.shaper = TrafficShaper(options)
        atexit.register(self.cleanup)
        signal.signal(signal.SIGTERM, self.signal_handler)
        signal.signal(signal.SIGINT, self.signal_handler)

    def run_testing(self):
        """Main testing flow"""
        import monotonic
        start_time = monotonic.monotonic()
        browser = None
        exit_file = os.path.join(self.root_path, 'exit')
        message_server = None
        if not self.options.android and not self.options.iOS:
            message_server = MessageServer()
            message_server.start()
        while not self.must_exit:
            try:
                if os.path.isfile(exit_file):
                    try:
                        os.remove(exit_file)
                    except Exception:
                        pass
                    self.must_exit = True
                    break
                if message_server is not None and self.options.exit > 0 and \
                        not message_server.is_ok():
                    logging.error("Message server not responding, exiting")
                    break
                if self.browsers.is_ready():
                    self.job = self.wpt.get_test()
                    if self.job is not None:
                        self.job['message_server'] = message_server
                        self.job['capture_display'] = self.capture_display
                        self.task = self.wpt.get_task(self.job)
                        while self.task is not None:
                            start = monotonic.monotonic()
                            try:
                                self.task['running_lighthouse'] = False
                                if self.job['type'] != 'lighthouse':
                                    self.run_single_test()
                                if self.task['run'] == 1 and not self.task['cached'] and \
                                        self.task['error'] is None and \
                                        'lighthouse' in self.job and self.job['lighthouse']:
                                    self.task['running_lighthouse'] = True
                                    self.wpt.running_another_test(self.task)
                                    self.run_single_test()
                                elapsed = monotonic.monotonic() - start
                                logging.debug('Test run time: %0.3f sec', elapsed)
                            except Exception as err:
                                msg = ''
                                if err is not None and err.__str__() is not None:
                                    msg = err.__str__()
                                self.task['error'] = 'Unhandled exception running test: '\
                                    '{0}'.format(msg)
                                logging.exception("Unhandled exception running test: %s", msg)
                                traceback.print_exc(file=sys.stdout)
                            self.wpt.upload_task_result(self.task)
                            # Set up for the next run
                            self.task = self.wpt.get_task(self.job)
                if self.job is not None:
                    self.job = None
                else:
                    self.sleep(self.options.polling)
            except Exception as err:
                msg = ''
                if err is not None and err.__str__() is not None:
                    msg = err.__str__()
                if self.task is not None:
                    self.task['error'] = 'Unhandled exception preparing test: '\
                        '{0}'.format(msg)
                logging.exception("Unhandled exception: %s", msg)
                traceback.print_exc(file=sys.stdout)
                if browser is not None:
                    browser.on_stop_recording(None)
                    browser = None
            if self.options.exit > 0:
                run_time = (monotonic.monotonic() - start_time) / 60.0
                if run_time > self.options.exit:
                    break

    def run_single_test(self):
        """Run a single test run"""
        self.alive()
        browser = self.browsers.get_browser(self.job['browser'], self.job)
        if browser is not None:
            browser.prepare(self.job, self.task)
            browser.launch(self.job, self.task)
            if self.shaper.configure(self.job):
                try:
                    if self.task['running_lighthouse']:
                        browser.run_lighthouse_test(self.task)
                    else:
                        browser.run_task(self.task)
                except Exception as err:
                    msg = ''
                    if err is not None and err.__str__() is not None:
                        msg = err.__str__()
                    self.task['error'] = 'Unhandled exception in test run: '\
                        '{0}'.format(msg)
                    logging.exception("Unhandled exception in test run: %s", msg)
                    traceback.print_exc(file=sys.stdout)
            else:
                self.task['error'] = "Error configuring traffic-shaping"
            self.shaper.reset()
            browser.stop(self.job, self.task)
            # Delete the browser profile if needed
            if self.task['cached'] or self.job['fvonly']:
                browser.clear_profile(self.task)
        else:
            err = "Invalid browser - {0}".format(self.job['browser'])
            logging.critical(err)
            self.task['error'] = err
        browser = None

    def signal_handler(self, *_):
        """Ctrl+C handler"""
        if self.must_exit:
            exit(1)
        if self.job is None:
            print "Exiting..."
        else:
            print "Will exit after test completes.  Hit Ctrl+C again to exit immediately"
        self.must_exit = True

    def cleanup(self):
        """Do any cleanup that needs to be run regardless of how we exit."""
        logging.debug('Cleaning up')
        self.shaper.remove()
        if self.xvfb is not None:
            self.xvfb.stop()
        if self.adb is not None:
            self.adb.stop()
        if self.ios is not None:
            self.ios.disconnect()

    def sleep(self, seconds):
        """Sleep wrapped in an exception handler to properly deal with Ctrl+C"""
        try:
            time.sleep(seconds)
        except IOError:
            pass

    def wait_for_idle(self, timeout=30):
        """Wait for the system to go idle for at least 2 seconds"""
        import monotonic
        import psutil
        logging.debug("Waiting for Idle...")
        cpu_count = psutil.cpu_count()
        if cpu_count > 0:
            target_pct = 50. / float(cpu_count)
            idle_start = None
            end_time = monotonic.monotonic() + timeout
            idle = False
            while not idle and monotonic.monotonic() < end_time:
                self.alive()
                check_start = monotonic.monotonic()
                pct = psutil.cpu_percent(interval=0.5)
                if pct <= target_pct:
                    if idle_start is None:
                        idle_start = check_start
                    if monotonic.monotonic() - idle_start > 2:
                        idle = True
                else:
                    idle_start = None

    def alive(self):
        """Touch a watchdog file indicating we are still alive"""
        if self.options.alive:
            with open(self.options.alive, 'a'):
                os.utime(self.options.alive, None)

    def startup(self):
        """Validate that all of the external dependencies are installed"""
        ret = True
        self.alive()

        try:
            import dns.resolver as _
        except ImportError:
            print "Missing dns module. Please run 'pip install dnspython'"
            ret = False

        try:
            import monotonic as _
        except ImportError:
            print "Missing monotonic module. Please run 'pip install monotonic'"
            ret = False

        try:
            from PIL import Image as _
        except ImportError:
            print "Missing PIL module. Please run 'pip install pillow'"
            ret = False

        try:
            import psutil as _
        except ImportError:
            print "Missing psutil module. Please run 'pip install psutil'"
            ret = False

        try:
            import requests as _
        except ImportError:
            print "Missing requests module. Please run 'pip install requests'"
            ret = False

        try:
            subprocess.check_output(['python', '--version'])
        except Exception:
            print "Make sure python 2.7 is available in the path."
            ret = False

        try:
            subprocess.check_output('convert -version', shell=True)
        except Exception:
            print "Missing convert utility. Please install ImageMagick " \
                  "and make sure it is in the path."
            ret = False

        try:
            subprocess.check_output('mogrify -version', shell=True)
        except Exception:
            print "Missing mogrify utility. Please install ImageMagick " \
                  "and make sure it is in the path."
            ret = False

        # if we are on Linux and there is no display, enable xvfb by default
        if platform.system() == "Linux" and not self.options.android and \
                not self.options.iOS and 'DISPLAY' not in os.environ:
            self.options.xvfb = True

        if self.options.xvfb:
            try:
                from xvfbwrapper import Xvfb
                self.xvfb = Xvfb(width=1920, height=1200, colordepth=24)
                self.xvfb.start()
            except ImportError:
                print "Missing xvfbwrapper module. Please run 'pip install xvfbwrapper'"
                ret = False

        # Figure out which display to capture from
        if platform.system() == "Linux" and 'DISPLAY' in os.environ:
            logging.debug('Display: %s', os.environ['DISPLAY'])
            self.capture_display = os.environ['DISPLAY']
        elif platform.system() == "Darwin":
            proc = subprocess.Popen('ffmpeg -f avfoundation -list_devices true -i ""',
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
            _, err = proc.communicate()
            for line in err.splitlines():
                matches = re.search(r'\[(\d+)\] Capture screen', line)
                if matches:
                    self.capture_display = matches.group(1)
                    break
        elif platform.system() == "Windows":
            self.capture_display = 'desktop'

        if self.options.throttle:
            try:
                subprocess.check_output('sudo cgset -h', shell=True)
            except Exception:
                print "Missing cgroups, make sure cgroup-tools is installed."
                ret = False

        # Windows-specific imports
        if platform.system() == "Windows":
            try:
                import win32api as _
                import win32process as _
            except ImportError:
                print "Missing pywin32 module. Please run 'python -m pip install pypiwin32'"
                ret = False
        
        # Check the iOS install
        if self.ios is not None:
            ret = self.ios.check_install()

        if not self.options.android and not self.options.iOS:
            self.wait_for_idle(300)
        self.shaper.remove()
        if not self.shaper.install():
            print "Error configuring traffic shaping, make sure it is installed."
            ret = False

        if self.adb is not None:
            if not self.adb.start():
                print "Error configuring adb. Make sure it is installed and in the path."
                ret = False
        return ret


class HandleMessage(BaseHTTPRequestHandler):
    """Handle a single message from the extension"""
    protocol_version = 'HTTP/1.1'

    def __init__(self, server, *args):
        self.message_server = server
        try:
            BaseHTTPRequestHandler.__init__(self, *args)
            self.timeout = 10
        except Exception:
            pass

    def setup(self):
        BaseHTTPRequestHandler.setup(self)
        self.request.settimeout(60)

    def handle(self):
        try:
            BaseHTTPRequestHandler.handle(self)
        except Exception:
            pass

    def log_message(self, _, *args):
        return

    def _set_headers(self):
        """Basic response headers"""
        try:
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header("Content-length", '0')
            self.end_headers()
        except Exception:
            pass

    # pylint: disable=C0103
    def do_GET(self):
        """HTTP GET"""
        if self.path == '/ping':
            response = 'pong'
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.send_header("Content-length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
        else:
            self._set_headers()

    def do_HEAD(self):
        """HTTP HEAD"""
        self._set_headers()

    def do_POST(self):
        """HTTP POST"""
        import ujson as json
        try:
            content_len = int(self.headers.getheader('content-length', 0))
            messages = None
            if content_len > 0:
                self.rfile._sock.settimeout(10)
                messages = self.rfile.read(content_len)
            self._set_headers()
            if messages is not None:
                for line in messages.splitlines():
                    line = line.strip()
                    if len(line):
                        message = json.loads(line)
                        if 'body' not in message:
                            message['body'] = None
                        self.message_server.handle_message(message)
        except Exception:
            pass
    # pylint: enable=C0103

def handler_template(server):
    """Stub that lets us pass parameters to BaseHTTPRequestHandler"""
    return lambda *args: HandleMessage(server, *args)

class MessageServer(object):
    """Local HTTP server for interacting with the extension"""
    def __init__(self):
        self.server = None
        self.must_exit = False
        self.thread = None
        self.messages = Queue.Queue()
        self.__is_started = threading.Event()

    def get_message(self, timeout):
        """Get a single message from the queue"""
        message = self.messages.get(block=True, timeout=timeout)
        self.messages.task_done()
        return message

    def flush_messages(self):
        """Flush all of the pending messages"""
        try:
            while True:
                self.messages.get_nowait()
                self.messages.task_done()
        except Exception:
            pass

    def handle_message(self, message):
        """Add a received message to the queue"""
        self.messages.put(message)

    def start(self):
        """Start running the server in a background thread"""
        self.__is_started.clear()
        self.thread = threading.Thread(target=self.run)
        self.thread.daemon = True
        self.thread.start()
        self.__is_started.wait(timeout=30)

    def stop(self):
        """Stop running the server"""
        logging.debug("Shutting down extension server")
        self.must_exit = True
        if self.server is not None:
            try:
                self.server.shutdown()
            except Exception:
                logging.exception("Exception stopping extension server")
        self.thread = None
        logging.debug("Extension server stopped")

    def is_ok(self):
        """Check that the server is responding and restart it if necessary"""
        import requests
        import monotonic
        end_time = monotonic.monotonic() + 30
        server_ok = False
        while not server_ok and monotonic.monotonic() < end_time:
            try:
                response = requests.get('http://127.0.0.1:8888/ping', timeout=10)
                if response.text == 'pong':
                    server_ok = True
            except Exception:
                pass
            if not server_ok:
                time.sleep(5)
        return server_ok

    def run(self):
        """Main server loop"""
        handler = handler_template(self)
        logging.debug('Starting extension server on port 8888')
        try:
            self.server = HTTPServer(('127.0.0.1', 8888), handler)
            self.server.timeout = 10
            self.__is_started.set()
            self.server.serve_forever()
        except Exception:
            logging.exception("Message server main loop exception")
            self.__is_started.set()
        if self.server is not None:
            try:
                self.server.socket.close()
            except Exception:
                pass

def parse_ini(ini):
    """Parse an ini file and convert it to a dictionary"""
    import ConfigParser
    ret = None
    if os.path.isfile(ini):
        parser = ConfigParser.SafeConfigParser()
        parser.read(ini)
        ret = {}
        for section in parser.sections():
            ret[section] = {}
            for item in parser.items(section):
                ret[section][item[0]] = item[1]
        if not ret:
            ret = None
    return ret

def getWindowsBuild():
    """Get the current Windows build number from the registry"""
    key = 'HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion'
    val = 'CurrentBuild'
    output = os.popen('REG QUERY "{0}" /V "{1}"'.format(key, val)).read()
    return int(output.strip().split(' ')[-1])

def find_browsers():
    """Find the various known-browsers in case they are not explicitly configured"""
    browsers = parse_ini(os.path.join(os.path.dirname(__file__), "browsers.ini"))
    if browsers is None:
        browsers = {}
    plat = platform.system()
    if plat == "Windows":
        local_appdata = os.getenv('LOCALAPPDATA')
        program_files = os.getenv('ProgramFiles')
        program_files_x86 = os.getenv('ProgramFiles(x86)')
        if program_files is not None and 'Chrome' not in browsers:
            chrome_path = os.path.join(program_files, 'Google', 'Chrome',
                                       'Application', 'chrome.exe')
            if os.path.isfile(chrome_path):
                browsers['Chrome'] = {'exe': chrome_path}
        if program_files_x86 is not None and 'Chrome' not in browsers:
            chrome_path = os.path.join(program_files_x86, 'Google', 'Chrome',
                                       'Application', 'chrome.exe')
            if os.path.isfile(chrome_path):
                browsers['Chrome'] = {'exe': chrome_path}
        if local_appdata is not None and 'Chrome' not in browsers:
            chrome_path = os.path.join(local_appdata, 'Google', 'Chrome',
                                       'Application', 'chrome.exe')
            if os.path.isfile(chrome_path):
                browsers['Chrome'] = {'exe': chrome_path}
        if local_appdata is not None and 'Canary' not in browsers:
            canary_path = os.path.join(local_appdata, 'Google', 'Chrome SxS',
                                       'Application', 'chrome.exe')
            if os.path.isfile(canary_path):
                browsers['Canary'] = {'exe': canary_path}
        # Firefox browsers
        if program_files_x86 is not None and 'Firefox' not in browsers:
            firefox_path = os.path.join(program_files_x86, 'Mozilla Firefox',
                                        'firefox.exe')
            if os.path.isfile(firefox_path):
                browsers['Firefox'] = {'exe': firefox_path, 'type': 'Firefox'}
        if program_files_x86 is not None and 'Firefox Nightly' not in browsers:
            firefox_path = os.path.join(program_files_x86, 'Nightly', 'firefox.exe')
            if os.path.isfile(firefox_path):
                browsers['Firefox Nightly'] = {'exe': firefox_path, 'type': 'Firefox'}
        # Microsoft Edge
        edge = None
        build = getWindowsBuild()
        if build >= 10240:
            edge_exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'internal',
                                    'support', 'edge', 'current', 'MicrosoftWebDriver.exe')
            if not os.path.isfile(edge_exe):
                if build >= 15000:
                    edge_version = 15
                elif build >= 14000:
                    edge_version = 14
                elif build >= 10586:
                    edge_version = 13
                else:
                    edge_version = 12
                edge_exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'internal',
                                        'support', 'edge', str(edge_version),
                                        'MicrosoftWebDriver.exe')
            if os.path.isfile(edge_exe):
                edge = {'exe': edge_exe}
        if edge is not None:
            edge['type'] = 'Edge'
            if 'Microsoft Edge' not in browsers:
                browsers['Microsoft Edge'] = dict(edge)
            if 'Edge' not in browsers:
                browsers['Edge'] = dict(edge)
    elif plat == "Linux":
        chrome_path = '/opt/google/chrome/chrome'
        if 'Chrome' not in browsers and os.path.isfile(chrome_path):
            browsers['Chrome'] = {'exe': chrome_path}
        beta_path = '/opt/google/chrome-beta/chrome'
        if 'Chrome Beta' not in browsers and os.path.isfile(beta_path):
            browsers['Chrome Beta'] = {'exe': beta_path}
        # google-chrome-unstable is the closest thing to Canary for Linux
        canary_path = '/opt/google/chrome-unstable/chrome'
        if os.path.isfile(canary_path):
            if 'Chrome Dev' not in browsers:
                browsers['Chrome Dev'] = {'exe': canary_path}
            if 'Chrome Canary' not in browsers:
                browsers['Chrome Canary'] = {'exe': canary_path}
            if 'Canary' not in browsers:
                browsers['Canary'] = {'exe': canary_path}
        # Firefox browsers
        firefox_path = '/usr/lib/firefox/firefox'
        if 'Firefox' not in browsers and os.path.isfile(firefox_path):
            browsers['Firefox'] = {'exe': firefox_path, 'type': 'Firefox'}
        nightly_path = '/usr/lib/firefox-trunk/firefox-trunk'
        if 'Firefox Nightly' not in browsers and os.path.isfile(nightly_path):
            browsers['Firefox Nightly'] = {'exe': nightly_path, 'type': 'Firefox'}
    elif plat == "Darwin":
        chrome_path = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
        if 'Chrome' not in browsers and os.path.isfile(chrome_path):
            browsers['Chrome'] = {'exe': chrome_path}
        canary_path = '/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary'
        if os.path.isfile(canary_path):
            if 'Chrome Dev' not in browsers:
                browsers['Chrome Dev'] = {'exe': canary_path}
            if 'Chrome Canary' not in browsers:
                browsers['Chrome Canary'] = {'exe': canary_path}
            if 'Canary' not in browsers:
                browsers['Canary'] = {'exe': canary_path}
        firefox_path = '/Applications/Firefox.app/Contents/MacOS/firefox'
        if 'Firefox' not in browsers and os.path.isfile(firefox_path):
            browsers['Firefox'] = {'exe': firefox_path, 'type': 'Firefox'}
        nightly_path = '/Applications/FirefoxNightly.app/Contents/MacOS/firefox'
        if 'Firefox Nightly' not in browsers and os.path.isfile(nightly_path):
            browsers['Firefox Nightly'] = {'exe': nightly_path, 'type': 'Firefox'}
    return browsers

def main():
    """Startup and initialization"""
    import argparse
    parser = argparse.ArgumentParser(description='WebPageTest Agent.', prog='wpt-agent')
    # Basic agent config
    parser.add_argument('-v', '--verbose', action='count',
                        help="Increase verbosity (specify multiple times for more)."
                        " -vvvv for full debug output.")
    parser.add_argument('--name', help="Agent name (for the work directory).")
    parser.add_argument('--exit', type=int, default=0,
                        help='Exit after the specified number of minutes.\n'\
                        '    Useful for running in a shell script that does some maintenence\n'\
                        '    or updates periodically (like hourly).')
    parser.add_argument('--dockerized', action='store_true', default=False,
                        help="Agent is running in a docker container.")
    parser.add_argument('--ec2', action='store_true', default=False,
                        help="Load config settings from EC2 user data.")
    parser.add_argument('--gce', action='store_true', default=False,
                        help="Load config settings from GCE user data.")
    parser.add_argument('--alive',
                        help="Watchdog file to update when successfully connected.")
    parser.add_argument('--log',
                        help="Log critical errors to the given file.")

    # Video capture/display settings
    parser.add_argument('--xvfb', action='store_true', default=False,
                        help="Use an xvfb virtual display (Linux only).")
    parser.add_argument('--fps', type=int, choices=xrange(1, 61), default=10,
                        help='Video capture frame rate (defaults to 10). '\
                             'Valid range is 1-60 (Linux only).')

    # Server/location configuration
    parser.add_argument('--server',
                        help="URL for WebPageTest work (i.e. http://www.webpagetest.org/work/).")
    parser.add_argument('--validcertificate', action='store_true', default=False,
                        help="Validate server certificates (HTTPS server, defaults to False).")
    parser.add_argument('--location',
                        help="Location ID (as configured in locations.ini on the server).")
    parser.add_argument('--key', help="Location key (optional).")
    parser.add_argument('--polling', type=int, default=5,
                        help='Polling interval for work (defaults to 5 seconds).')

    # Traffic-shaping options (defaults to host-based)
    parser.add_argument('--shaper', help='Override default traffic shaper. '\
                        'Current supported values are:\n'\
                        '    none - Disable traffic-shaping (i.e. when root is not available)\n.'\
                        '    netem,<interface> - Use NetEm for bridging rndis traffic '\
                        '(specify outbound interface).  i.e. --shaper netem,eth0\n'\
                        '    remote,<server>,<down pipe>,<up pipe> - Connect to the remote server '\
                        'over ssh and use pre-configured dummynet pipes (ssh keys for root user '\
                        'should be pre-authorized).')

    # CPU Throttling
    parser.add_argument('--throttle', action='store_true', default=False,
                        help='Enable cgroup-based CPU throttling for mobile emulation '\
                        '(Linux only).')

    # Android options
    parser.add_argument('--android', action='store_true', default=False,
                        help="Run tests on an attached android device.")
    parser.add_argument('--device',
                        help="Device ID (only needed if more than one android device attached).")
    parser.add_argument('--rndis',
                        help="Enable reverse-tethering over rndis.  Valid options are:\n"\
                        "    dhcp: Configure interface for DHCP\n"\
                        "    <ip>/<network>,<gateway>,<dns1>,<dns2>: Static Address.  \n"\
                        "        i.e. 192.168.0.8/24,192.168.0.1,8.8.8.8,8.8.4.4")
    parser.add_argument('--simplert',
                        help="Use SimpleRT for reverse-tethering.  The APK should be installed "\
                        "manually (adb install simple-rt/simple-rt-1.1.apk) and tested once "\
                        "manually (./simple-rt -i eth0 then disconnect and re-connect phone) "\
                        "to dismiss any system dialogs.  The ethernet interface and DNS server "\
                        "should be passed as options:\n"\
                        "    <interface>,<dns1>: i.e. --simplert eth0,192.168.0.1")

    # iOS options
    parser.add_argument('--iOS', action='store_true', default=False,
                        help="Run tests on an attached iOS device "\
                        "(specify serial number in --device).")
    parser.add_argument('--list', action='store_true', default=False,
                        help="List available iOS devices.")

    # Options for authenticating the agent with the server
    parser.add_argument('--username',
                        help="User name if using HTTP Basic auth with WebPageTest server.")
    parser.add_argument('--password',
                        help="Password if using HTTP Basic auth with WebPageTest server.")
    parser.add_argument('--cert', help="Client certificate if using certificates to "\
                        "authenticate the WebPageTest server connection.")
    parser.add_argument('--certkey', help="Client-side private key (if not embedded in the cert).")
    options, _ = parser.parse_known_args()

    # Make sure we are running python 2.7.11 or newer (required for Windows 8.1)
    if platform.system() == "Windows":
        if sys.version_info[0] != 2 or \
                sys.version_info[1] != 7 or \
                sys.version_info[2] < 11:
            print "Requires python 2.7.11 (2.7.11 or later)"
            exit(1)
    elif sys.version_info[0] != 2 or sys.version_info[1] != 7:
        print "Requires python 2.7"
        exit(1)

    if options.list:
        from internal.ios_device import iOSDevice
        iOS = iOSDevice()
        devices = iOS.get_devices()
        print "Available iOS devices:"
        for device in devices:
            print device
        exit(1)

    # Set up logging
    log_level = logging.CRITICAL
    if options.verbose == 1:
        log_level = logging.ERROR
    elif options.verbose == 2:
        log_level = logging.WARNING
    elif options.verbose == 3:
        log_level = logging.INFO
    elif options.verbose >= 4:
        log_level = logging.DEBUG
    logging.basicConfig(level=log_level, format="%(asctime)s.%(msecs)03d - %(message)s",
                        datefmt="%H:%M:%S")

    if options.log:
        err_log = logging.handlers.RotatingFileHandler(options.log, maxBytes=1000000,
                                                       backupCount=5, delay=True)
        err_log.setLevel(logging.ERROR)
        logging.getLogger().addHandler(err_log)

    browsers = None
    if not options.android and not options.iOS:
        browsers = find_browsers()
        if len(browsers) == 0:
            print "No browsers configured. Check that browsers.ini is present and correct."
            exit(1)

    agent = WPTAgent(options, browsers)
    if agent.startup():
        #Create a work directory relative to where we are running
        print "Running agent, hit Ctrl+C to exit"
        agent.run_testing()
        print "Done"

if __name__ == '__main__':
    main()
