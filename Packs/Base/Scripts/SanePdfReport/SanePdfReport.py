import demistomock as demisto
from CommonServerPython import *
from CommonServerUserPython import *
import traceback

import os
import shlex
import base64
import random
import string
import subprocess
from pathlib import Path

WORKING_DIR = Path("/app")
INPUT_FILE_PATH = 'sample.json'
OUTPUT_FILE_PATH = 'out{id}.pdf'
DISABLE_LOGOS = True  # Bugfix before sane-reports can work with image files.


def random_string(size=10):
    return ''.join(
        random.choices(string.ascii_uppercase + string.digits, k=size))


try:
    sane_json_b64 = demisto.args().get('sane_pdf_report_base64', '').encode(
        'utf-8')
    orientation = demisto.args().get('orientation', 'portrait')
    resourceTimeout = demisto.args().get('resourceTimeout', '4000')
    reportType = demisto.args().get('reportType', 'pdf')
    headerLeftImage = demisto.args().get('customerLogo', '')
    headerRightImage = demisto.args().get('demistoLogo', '')
    pageSize = demisto.args().get('paperSize', 'letter')
    disableHeaders = demisto.args().get('disableHeaders', '')

    # Note: After headerRightImage the empty one is for legacy argv in server.js
    extra_cmd = f"{orientation} {resourceTimeout} {reportType} " + \
                f'"{headerLeftImage}" "{headerRightImage}" "" ' + \
                f'"{pageSize}" "{disableHeaders}"'

    # Generate a random input file so we won't override on concurrent usage
    input_id = random_string()
    input_file = INPUT_FILE_PATH.format(id=input_id)

    with open(WORKING_DIR / input_file, 'wb') as f:
        f.write(base64.b64decode(sane_json_b64))

    # Generate a random output file so we won't override on concurrent usage
    output_id = random_string()
    output_file = OUTPUT_FILE_PATH.format(id=output_id)

    cmd = ['./reportsServer', input_file, output_file, 'dist'] + shlex.split(
        extra_cmd)

    # Logging things for debugging
    params = f'[orientation="{orientation}",' \
        f' resourceTimeout="{resourceTimeout}",' \
        f' reportType="{reportType}", headerLeftImage="{headerLeftImage}",' \
        f' headerRightImage="{headerRightImage}", pageSize="{pageSize}",' \
        f' disableHeaders="{disableHeaders}" '
    LOG(f"Sane-pdf parameters: {params}]")
    cmd_string = " ".join(cmd)
    LOG(f"Sane-pdf cmd: {cmd_string}")
    LOG.print_log()

    # Execute the report creation
    out = subprocess.check_output(cmd, cwd=WORKING_DIR,
                                  stderr=subprocess.STDOUT)
    LOG(f"Sane-pdf output: {str(out)}")

    abspath_output_file = WORKING_DIR / output_file
    with open(abspath_output_file, 'rb') as f:
        encoded = base64.b64encode(f.read()).decode('utf-8', 'ignore')

    os.remove(abspath_output_file)
    return_outputs(readable_output='Successfully generated pdf',
                   outputs={}, raw_response={'data': encoded})

except subprocess.CalledProcessError as e:
    tb = traceback.format_exc()
    wrap = "=====sane-pdf-reports error====="
    err = f'{wrap}\n{tb}{wrap}, process error: {e.output}\n'
    return_error(f'[SanePdfReports Automation Error] - {err}')

except Exception:
    tb = traceback.format_exc()
    wrap = "=====sane-pdf-reports error====="
    err = f'{wrap}\n{tb}{wrap}\n'
    return_error(f'[SanePdfReports Automation Error] - {err}')


def parseList():
    return


parseList()
