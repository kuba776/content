from __future__ import print_function
import os
import re
import sys
import json
import time
import argparse
import threading
import subprocess
import traceback
from time import sleep
import datetime
from distutils.version import LooseVersion

import pytz

from google.cloud import storage
from google.api_core.exceptions import PreconditionFailed
from queue import Queue
from contextlib import contextmanager

import urllib3
import requests
import demisto_client.demisto_api
from demisto_client.demisto_api.rest import ApiException
from slackclient import SlackClient

from Tests.Marketplace.marketplace_services import init_storage_client
from Tests.mock_server import MITMProxy, AMIConnection
from Tests.test_integration import Docker, test_integration, disable_all_integrations
from Tests.test_dependencies import get_used_integrations, get_tests_allocation_for_threads
from demisto_sdk.commands.common.constants import RUN_ALL_TESTS_FORMAT, FILTER_CONF, PB_Status
from demisto_sdk.commands.common.tools import print_color, print_error, print_warning, \
    LOG_COLORS, str2bool

# Disable insecure warnings
urllib3.disable_warnings()

SERVER_URL = "https://{}"
INTEGRATIONS_CONF = "./Tests/integrations_file.txt"

FAILED_MATCH_INSTANCE_MSG = "{} Failed to run.\n There are {} instances of {}, please select one of them by using " \
                            "the instance_name argument in conf.json. The options are:\n{}"

SERVICE_RESTART_TIMEOUT = 300
SERVICE_RESTART_POLLING_INTERVAL = 5
LOCKS_PATH = 'content-locks'
BUCKET_NAME = os.environ.get('GCS_ARTIFACTS_BUCKET')
CIRCLE_BUILD_NUM = os.environ.get('CIRCLE_BUILD_NUM')
WORKFLOW_ID = os.environ.get('CIRCLE_WORKFLOW_ID')
CIRCLE_STATUS_TOKEN = os.environ.get('CIRCLECI_STATUS_TOKEN')
SLACK_MEM_CHANNEL_ID = 'CM55V7J8K'


def options_handler():
    parser = argparse.ArgumentParser(description='Utility for batch action on incidents')
    parser.add_argument('-k', '--apiKey', help='The Demisto API key for the server', required=True)
    parser.add_argument('-s', '--server', help='The server URL to connect to')
    parser.add_argument('-c', '--conf', help='Path to conf file', required=True)
    parser.add_argument('-e', '--secret', help='Path to secret conf file')
    parser.add_argument('-n', '--nightly', type=str2bool, help='Run nightly tests')
    parser.add_argument('-t', '--slack', help='The token for slack', required=True)
    parser.add_argument('-a', '--circleci', help='The token for circleci', required=True)
    parser.add_argument('-b', '--buildNumber', help='The build number', required=True)
    parser.add_argument('-g', '--buildName', help='The build name', required=True)
    parser.add_argument('-p', '--private', help='Is the build private.',type=str2bool, required=False, default=False)
    parser.add_argument('-sa', '--service_account', help="Path to GCS service account.", required=False)
    parser.add_argument('-i', '--isAMI', type=str2bool, help='is AMI build or not', default=False)
    parser.add_argument('-m', '--memCheck', type=str2bool,
                        help='Should trigger memory checks or not. The slack channel to check the data is: '
                             'dmst_content_nightly_memory_data', default=False)
    parser.add_argument('-d', '--serverVersion', help='Which server version to run the '
                                                      'tests on(Valid only when using AMI)', default="NonAMI")
    parser.add_argument('-l', '--testsList', help='List of specific, comma separated'
                                                  'tests to run')

    options = parser.parse_args()
    tests_settings = TestsSettings(options)
    return tests_settings


class TestsSettings:
    def __init__(self, options):
        self.api_key = options.apiKey
        self.server = options.server
        self.conf_path = options.conf
        self.secret_conf_path = options.secret
        self.nightly = options.nightly
        self.slack = options.slack
        self.circleci = options.circleci
        self.buildNumber = options.buildNumber
        self.buildName = options.buildName
        self.isAMI = options.isAMI
        self.memCheck = options.memCheck
        self.serverVersion = options.serverVersion
        self.is_private = options.private
        self.service_account = options.service_account
        self.serverNumericVersion = None
        self.specific_tests_to_run = self.parse_tests_list_arg(options.testsList)
        self.is_local_run = (self.server is not None)

    @staticmethod
    def parse_tests_list_arg(tests_list):
        tests_to_run = tests_list.split(",") if tests_list else []
        return tests_to_run


class PrintJob:
    def __init__(self, message_to_print, print_function_to_execute, message_color=None):
        self.print_function_to_execute = print_function_to_execute
        self.message_to_print = message_to_print
        self.message_color = message_color

    def execute_print(self):
        if self.message_color:
            self.print_function_to_execute(self.message_to_print, self.message_color)
        else:
            self.print_function_to_execute(self.message_to_print)


class ParallelPrintsManager:

    def __init__(self, number_of_threads):
        self.threads_print_jobs = [[] for i in range(number_of_threads)]
        self.print_lock = threading.Lock()
        self.threads_last_update_times = [time.time() for i in range(number_of_threads)]

    def should_update_thread_status(self, thread_index):
        current_time = time.time()
        thread_last_update = self.threads_last_update_times[thread_index]
        return current_time - thread_last_update > 300

    def add_print_job(self, message_to_print, print_function_to_execute, thread_index, message_color=None,
                      include_timestamp=False):
        if include_timestamp:
            message_to_print = f'[{datetime.datetime.now(datetime.timezone.utc)}] {message_to_print}'

        print_job = PrintJob(message_to_print, print_function_to_execute, message_color=message_color)
        self.threads_print_jobs[thread_index].append(print_job)
        if self.should_update_thread_status(thread_index):
            print("Thread {} is still running.".format(thread_index))
            self.threads_last_update_times[thread_index] = time.time()

    def execute_thread_prints(self, thread_index):
        self.print_lock.acquire()
        prints_to_execute = self.threads_print_jobs[thread_index]
        for print_job in prints_to_execute:
            print_job.execute_print()
        self.print_lock.release()
        self.threads_print_jobs[thread_index] = []


class TestsDataKeeper:

    def __init__(self):
        self.succeeded_playbooks = []
        self.failed_playbooks = []
        self.skipped_tests = []
        self.skipped_integrations = []
        self.rerecorded_tests = []
        self.empty_files = []
        self.unmockable_integrations = {}

    def add_tests_data(self, succeed_playbooks, failed_playbooks, skipped_tests, skipped_integration,
                       unmockable_integrations):
        # Using multiple appends and not extend since append is guaranteed to be thread safe
        for playbook in succeed_playbooks:
            self.succeeded_playbooks.append(playbook)
        for playbook in failed_playbooks:
            self.failed_playbooks.append(playbook)
        for playbook in skipped_tests:
            self.skipped_tests.append(playbook)
        for playbook in skipped_integration:
            self.skipped_integrations.append(playbook)
        for playbook_id, reason in unmockable_integrations.items():
            self.unmockable_integrations[playbook_id] = reason

    def add_proxy_related_test_data(self, proxy):
        # Using multiple appends and not extend since append is guaranteed to be thread safe
        for playbook_id in proxy.rerecorded_tests:
            self.rerecorded_tests.append(playbook_id)
        for playbook_id in proxy.empty_files:
            self.empty_files.append(playbook_id)


def print_test_summary(tests_data_keeper, is_ami=True):
    succeed_playbooks = tests_data_keeper.succeeded_playbooks
    failed_playbooks = tests_data_keeper.failed_playbooks
    skipped_tests = tests_data_keeper.skipped_tests
    unmocklable_integrations = tests_data_keeper.unmockable_integrations
    skipped_integration = tests_data_keeper.skipped_integrations
    rerecorded_tests = tests_data_keeper.rerecorded_tests
    empty_files = tests_data_keeper.empty_files

    succeed_count = len(succeed_playbooks)
    failed_count = len(failed_playbooks)
    skipped_count = len(skipped_tests)
    rerecorded_count = len(rerecorded_tests) if is_ami else 0
    empty_mocks_count = len(empty_files) if is_ami else 0
    unmocklable_integrations_count = len(unmocklable_integrations)
    print('\nTEST RESULTS:')
    tested_playbooks_message = '\t Number of playbooks tested - ' + str(succeed_count + failed_count)
    print(tested_playbooks_message)
    succeeded_playbooks_message = '\t Number of succeeded tests - ' + str(succeed_count)
    print_color(succeeded_playbooks_message, LOG_COLORS.GREEN)

    if failed_count > 0:
        failed_tests_message = '\t Number of failed tests - ' + str(failed_count) + ':'
        print_error(failed_tests_message)
        for playbook_id in failed_playbooks:
            print_error('\t - ' + playbook_id)

    if rerecorded_count > 0:
        recording_warning = '\t Tests with failed playback and successful re-recording - ' + str(rerecorded_count) + ':'
        print_warning(recording_warning)
        for playbook_id in rerecorded_tests:
            print_warning('\t - ' + playbook_id)

    if empty_mocks_count > 0:
        empty_mock_successes_msg = '\t Successful tests with empty mock files - ' + str(empty_mocks_count) + ':'
        print(empty_mock_successes_msg)
        proxy_explanation = '\t (either there were no http requests or no traffic is passed through the proxy.\n' \
                            '\t Investigate the playbook and the integrations.\n' \
                            '\t If the integration has no http traffic, add to unmockable_integrations in conf.json)'
        print(proxy_explanation)
        for playbook_id in empty_files:
            print('\t - ' + playbook_id)

    if len(skipped_integration) > 0:
        skipped_integrations_warning = '\t Number of skipped integration - ' + str(len(skipped_integration)) + ':'
        print_warning(skipped_integrations_warning)
        for playbook_id in skipped_integration:
            print_warning('\t - ' + playbook_id)

    if skipped_count > 0:
        skipped_tests_warning = '\t Number of skipped tests - ' + str(skipped_count) + ':'
        print_warning(skipped_tests_warning)
        for playbook_id in skipped_tests:
            print_warning('\t - ' + playbook_id)

    if unmocklable_integrations_count > 0:
        unmockable_warning = '\t Number of unmockable integrations - ' + str(unmocklable_integrations_count) + ':'
        print_warning(unmockable_warning)
        for playbook_id, reason in unmocklable_integrations.items():
            print_warning('\t - ' + playbook_id + ' - ' + reason)


def update_test_msg(integrations, test_message):
    if integrations:
        integrations_names = [integration['name'] for integration in
                              integrations]
        test_message = test_message + ' with integration(s): ' + ','.join(
            integrations_names)

    return test_message


def turn_off_telemetry(xsoar_client):
    """
    Turn off telemetry on the AMI instance

    :param xsoar_client: Preconfigured client for the XSOAR instance
    :return: None
    """

    body, status_code, _ = demisto_client.generic_request_func(self=xsoar_client, method='POST',
                                                               path='/telemetry?status=notelemetry')

    if status_code != 200:
        print_error('Request to turn off telemetry failed with status code "{}"\n{}'.format(status_code, body))
        sys.exit(1)


def reset_containers(server, demisto_user, demisto_pass, prints_manager, thread_index):
    prints_manager.add_print_job('Resetting containers', print, thread_index)
    client = demisto_client.configure(base_url=server, username=demisto_user, password=demisto_pass, verify_ssl=False)
    body, status_code, _ = demisto_client.generic_request_func(self=client, method='POST',
                                                               path='/containers/reset')
    if status_code != 200:
        error_msg = 'Request to reset containers failed with status code "{}"\n{}'.format(status_code, body)
        prints_manager.add_print_job(error_msg, print_error, thread_index)
        prints_manager.execute_thread_prints(thread_index)
        sys.exit(1)
    sleep(10)


def has_unmockable_integration(integrations, unmockable_integrations):
    return list(set(x['name'] for x in integrations).intersection(unmockable_integrations.keys()))


def get_docker_limit():
    process = subprocess.Popen(['cat', '/sys/fs/cgroup/memory/memory.limit_in_bytes'], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
    stdout, stderr = process.communicate()
    return stdout, stderr


def get_docker_processes_data():
    process = subprocess.Popen(['ps', 'aux'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    stdout, stderr = process.communicate()
    return stdout, stderr


def get_docker_memory_data():
    process = subprocess.Popen(['cat', '/sys/fs/cgroup/memory/memory.usage_in_bytes'], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
    stdout, stderr = process.communicate()
    return stdout, stderr


def send_slack_message(slack, chanel, text, user_name, as_user):
    sc = SlackClient(slack)
    sc.api_call(
        "chat.postMessage",
        channel=chanel,
        username=user_name,
        as_user=as_user,
        text=text,
        mrkdwn='true'
    )


def run_test_logic(conf_json_test_details, tests_queue, tests_settings, c, failed_playbooks, integrations, playbook_id,
                   succeed_playbooks, test_message, test_options, slack, circle_ci, build_number, server_url,
                   build_name, prints_manager, thread_index=0, is_mock_run=False):
    # with acquire_test_lock(integrations,
    #                        test_options.get('timeout'),
    #                        prints_manager,
    #                        thread_index,
    #                        tests_settings) as lock:
    #     if lock:
    status, inc_id = test_integration(c, server_url, integrations, playbook_id, prints_manager, test_options,
                                      is_mock_run, thread_index=thread_index)
    # c.api_client.pool.close()
    if status == PB_Status.COMPLETED:
        prints_manager.add_print_job('PASS: {} succeed'.format(test_message), print_color, thread_index,
                                     message_color=LOG_COLORS.GREEN)
        succeed_playbooks.append(playbook_id)

    elif status == PB_Status.NOT_SUPPORTED_VERSION:
        not_supported_version_message = 'PASS: {} skipped - not supported version'.format(test_message)
        prints_manager.add_print_job(not_supported_version_message, print, thread_index)
        succeed_playbooks.append(playbook_id)

    else:
        error_message = 'Failed: {} failed'.format(test_message)
        prints_manager.add_print_job(error_message, print_error, thread_index)
        playbook_id_with_mock = playbook_id
        if not is_mock_run:
            playbook_id_with_mock += " (Mock Disabled)"
        failed_playbooks.append(playbook_id_with_mock)
        if not tests_settings.is_local_run:
            notify_failed_test(slack, circle_ci, playbook_id, build_number, inc_id, server_url, build_name)

    succeed = status in (PB_Status.COMPLETED, PB_Status.NOT_SUPPORTED_VERSION)
        # else:
        #     tests_queue.put(conf_json_test_details)
        #     succeed = False

    return succeed


# run the test using a real instance, record traffic.
def run_and_record(conf_json_test_details, tests_queue, tests_settings, c, proxy, failed_playbooks, integrations,
                   playbook_id, succeed_playbooks, test_message, test_options, slack, circle_ci, build_number,
                   server_url, build_name, prints_manager, thread_index=0):
    proxy.set_tmp_folder()
    proxy.start(playbook_id, record=True, thread_index=thread_index, prints_manager=prints_manager)
    succeed = run_test_logic(conf_json_test_details, tests_queue, tests_settings, c, failed_playbooks, integrations,
                             playbook_id, succeed_playbooks, test_message, test_options, slack, circle_ci, build_number,
                             server_url, build_name, prints_manager, thread_index=thread_index, is_mock_run=True)
    proxy.stop(thread_index=thread_index, prints_manager=prints_manager)
    if succeed:
        proxy.clean_mock_file(playbook_id, thread_index=thread_index, prints_manager=prints_manager)
        proxy.move_mock_file_to_repo(playbook_id, thread_index=thread_index, prints_manager=prints_manager)

    proxy.set_repo_folder()
    return succeed


def mock_run(conf_json_test_details, tests_queue, tests_settings, c, proxy, failed_playbooks, integrations,
             playbook_id, succeed_playbooks, test_message, test_options, slack, circle_ci, build_number, server_url,
             build_name, start_message, prints_manager, thread_index=0):
    rerecord = False

    if proxy.has_mock_file(playbook_id):
        start_mock_message = '{} (Mock: Playback)'.format(start_message)
        prints_manager.add_print_job(start_mock_message, print, thread_index, include_timestamp=True)
        proxy.start(playbook_id, thread_index=thread_index, prints_manager=prints_manager)
        # run test
        status, _ = test_integration(c, server_url, integrations, playbook_id, prints_manager, test_options,
                                     is_mock_run=True, thread_index=thread_index)
        # use results
        proxy.stop(thread_index=thread_index, prints_manager=prints_manager)
        if status == PB_Status.COMPLETED:
            succeed_message = 'PASS: {} succeed'.format(test_message)
            prints_manager.add_print_job(succeed_message, print_color, thread_index, LOG_COLORS.GREEN)
            succeed_playbooks.append(playbook_id)
            end_mock_message = f'------ Test {test_message} end ------\n'
            prints_manager.add_print_job(end_mock_message, print, thread_index, include_timestamp=True)
            return

        if status == PB_Status.NOT_SUPPORTED_VERSION:
            not_supported_version_message = 'PASS: {} skipped - not supported version'.format(test_message)
            prints_manager.add_print_job(not_supported_version_message, print, thread_index)
            succeed_playbooks.append(playbook_id)
            end_mock_message = f'------ Test {test_message} end ------\n'
            prints_manager.add_print_job(end_mock_message, print, thread_index, include_timestamp=True)
            return

        if status == PB_Status.FAILED_DOCKER_TEST:
            error_message = 'Failed: {} failed'.format(test_message)
            prints_manager.add_print_job(error_message, print_error, thread_index)
            failed_playbooks.append(playbook_id)
            end_mock_message = f'------ Test {test_message} end ------\n'
            prints_manager.add_print_job(end_mock_message, print, thread_index, include_timestamp=True)
            return

        mock_failed_message = "Test failed with mock, recording new mock file. (Mock: Recording)"
        prints_manager.add_print_job(mock_failed_message, print, thread_index)
        rerecord = True
    else:
        mock_recording_message = start_message + ' (Mock: Recording)'
        prints_manager.add_print_job(mock_recording_message, print, thread_index, include_timestamp=True)

    # Mock recording - no mock file or playback failure.
    c = demisto_client.configure(base_url=c.api_client.configuration.host,
                                 api_key=c.api_client.configuration.api_key, verify_ssl=False)
    succeed = run_and_record(conf_json_test_details, tests_queue, tests_settings, c, proxy, failed_playbooks,
                             integrations, playbook_id, succeed_playbooks, test_message, test_options, slack, circle_ci,
                             build_number, server_url, build_name, prints_manager, thread_index=thread_index)

    if rerecord and succeed:
        proxy.rerecorded_tests.append(playbook_id)
    test_end_message = f'------ Test {test_message} end ------\n'
    prints_manager.add_print_job(test_end_message, print, thread_index, include_timestamp=True)


def run_test(conf_json_test_details, tests_queue, tests_settings, demisto_user, demisto_pass, proxy, failed_playbooks,
             integrations, unmockable_integrations, playbook_id, succeed_playbooks, test_message, test_options,
             slack, circle_ci, build_number, server_url, build_name, prints_manager, is_ami=True, thread_index=0, is_private=False):
    start_message = f'------ Test {test_message} start ------'
    client = demisto_client.configure(base_url=server_url, username=demisto_user, password=demisto_pass, verify_ssl=False)

    if not is_ami or (not integrations or has_unmockable_integration(integrations, unmockable_integrations)):
        prints_manager.add_print_job(start_message + ' (Mock: Disabled)', print, thread_index, include_timestamp=True)
        run_test_logic(conf_json_test_details, tests_queue, tests_settings, client, failed_playbooks, integrations,
                       playbook_id, succeed_playbooks, test_message, test_options, slack, circle_ci, build_number,
                       server_url, build_name, prints_manager, thread_index=thread_index)
        prints_manager.add_print_job('------ Test %s end ------\n' % (test_message,), print, thread_index,
                                     include_timestamp=True)

        return
    if not is_private:
        mock_run(conf_json_test_details, tests_queue, tests_settings, client, proxy, failed_playbooks, integrations,
                 playbook_id, succeed_playbooks, test_message, test_options, slack, circle_ci, build_number,
                 server_url, build_name, start_message, prints_manager, thread_index=thread_index)


def http_request(url, params_dict=None):
    try:
        res = requests.request("GET",
                               url,
                               verify=True,
                               params=params_dict,
                               )
        res.raise_for_status()

        return res.json()

    except Exception as e:
        raise e


def get_user_name_from_circle(circleci_token, build_number):
    url = "https://circleci.com/api/v1.1/project/github/demisto/content/{0}?circle-token={1}".format(build_number,
                                                                                                     circleci_token)
    res = http_request(url)

    user_details = res.get('user', {})
    return user_details.get('name', '')


def notify_failed_test(slack, circle_ci, playbook_id, build_number, inc_id, server_url, build_name):
    circle_user_name = get_user_name_from_circle(circle_ci, build_number)
    sc = SlackClient(slack)
    user_id = retrieve_id(circle_user_name, sc)

    text = "{0} - {1} Failed\n{2}".format(build_name, playbook_id, server_url) if inc_id == -1 \
        else "{0} - {1} Failed\n{2}/#/WorkPlan/{3}".format(build_name, playbook_id, server_url, inc_id)

    if user_id:
        sc.api_call(
            "chat.postMessage",
            channel=user_id,
            username="Content CircleCI",
            as_user="False",
            text=text
        )


def retrieve_id(circle_user_name, sc):
    user_id = ''
    res = sc.api_call('users.list')

    user_list = res.get('members', [])
    for user in user_list:
        profile = user.get('profile', {})
        name = profile.get('real_name_normalized', '')
        if name == circle_user_name:
            user_id = user.get('id', '')

    return user_id


def create_result_files(tests_data_keeper):
    failed_playbooks = tests_data_keeper.failed_playbooks
    skipped_integration = tests_data_keeper.skipped_integrations
    skipped_tests = tests_data_keeper.skipped_tests
    with open("./Tests/failed_tests.txt", "w") as failed_tests_file:
        failed_tests_file.write('\n'.join(failed_playbooks))
    with open('./Tests/skipped_tests.txt', "w") as skipped_tests_file:
        skipped_tests_file.write('\n'.join(skipped_tests))
    with open('./Tests/skipped_integrations.txt', "w") as skipped_integrations_file:
        skipped_integrations_file.write('\n'.join(skipped_integration))


def change_placeholders_to_values(placeholders_map, config_item):
    """Replaces placeholders in the object to their real values

    Args:
        placeholders_map: (dict)
             Dict that holds the real values to be replaced for each placeholder.
        config_item: (json object)
            Integration configuration object.

    Returns:
        dict. json object with the real configuration.
    """
    item_as_string = json.dumps(config_item)
    for key, value in placeholders_map.items():
        item_as_string = item_as_string.replace(key, value)
    return json.loads(item_as_string)


def set_integration_params(demisto_api_key, integrations, secret_params, instance_names, playbook_id,
                           prints_manager, placeholders_map, thread_index=0):
    for integration in integrations:
        integration_params = [change_placeholders_to_values(placeholders_map, item) for item
                              in secret_params if item['name'] == integration['name']]

        if integration_params:
            matched_integration_params = integration_params[0]
            if len(integration_params) != 1:
                found_matching_instance = False
                for item in integration_params:
                    if item.get('instance_name', 'Not Found') in instance_names:
                        matched_integration_params = item
                        found_matching_instance = True

                if not found_matching_instance:
                    optional_instance_names = [optional_integration.get('instance_name', 'None')
                                               for optional_integration in integration_params]
                    error_msg = FAILED_MATCH_INSTANCE_MSG.format(playbook_id, len(integration_params),
                                                                 integration['name'],
                                                                 '\n'.join(optional_instance_names))
                    prints_manager.add_print_job(error_msg, print_error, thread_index)
                    return False

            integration['params'] = matched_integration_params.get('params', {})
            integration['byoi'] = matched_integration_params.get('byoi', True)
            integration['instance_name'] = matched_integration_params.get('instance_name', integration['name'])
            integration['validate_test'] = matched_integration_params.get('validate_test', True)
        elif integration['name'] == 'Demisto REST API':
            integration['params'] = {
                'url': 'https://localhost',
                'apikey': demisto_api_key,
                'insecure': True,
            }

    return True


def collect_integrations(integrations_conf, skipped_integration, skipped_integrations_conf, nightly_integrations):
    integrations = []
    is_nightly_integration = False
    test_skipped_integration = []
    for integration in integrations_conf:
        if integration in skipped_integrations_conf.keys():
            skipped_integration.add("{0} - reason: {1}".format(integration, skipped_integrations_conf[integration]))
            test_skipped_integration.append(integration)

        if integration in nightly_integrations:
            is_nightly_integration = True

        # string description
        integrations.append({
            'name': integration,
            'params': {}
        })

    return test_skipped_integration, integrations, is_nightly_integration


def extract_filtered_tests(is_nightly):
    if is_nightly:
        # TODO: verify this response
        return [], False, True
    with open(FILTER_CONF, 'r') as filter_file:
        filtered_tests = filter_file.readlines()
        filtered_tests = [line.strip('\n') for line in filtered_tests]
        is_filter_configured = bool(filtered_tests)
        run_all = RUN_ALL_TESTS_FORMAT in filtered_tests

    return filtered_tests, is_filter_configured, run_all


def load_conf_files(conf_path, secret_conf_path):
    with open(conf_path) as data_file:
        conf = json.load(data_file)

    secret_conf = None
    if secret_conf_path:
        with open(secret_conf_path) as data_file:
            secret_conf = json.load(data_file)

    return conf, secret_conf


def run_test_scenario(tests_queue, tests_settings, t, proxy, default_test_timeout, skipped_tests_conf,
                      nightly_integrations, skipped_integrations_conf, skipped_integration, is_nightly,
                      run_all_tests, is_filter_configured, filtered_tests, skipped_tests, secret_params,
                      failed_playbooks, playbook_skipped_integration, unmockable_integrations,
                      succeed_playbooks, slack, circle_ci, build_number, server, build_name,
                      server_numeric_version, demisto_user, demisto_pass, demisto_api_key,
                      prints_manager, thread_index=0, is_ami=True, is_private=False):
    playbook_id = t['playbookID']
    nightly_test = t.get('nightly', False)
    integrations_conf = t.get('integrations', [])
    instance_names_conf = t.get('instance_names', [])

    test_message = 'playbook: ' + playbook_id

    test_options = {
        'timeout': t.get('timeout', default_test_timeout),
        'memory_threshold': t.get('memory_threshold', Docker.DEFAULT_CONTAINER_MEMORY_USAGE),
        'pid_threshold': t.get('pid_threshold', Docker.DEFAULT_CONTAINER_PIDS_USAGE)
    }

    if not isinstance(integrations_conf, list):
        integrations_conf = [integrations_conf, ]

    if not isinstance(instance_names_conf, list):
        instance_names_conf = [instance_names_conf, ]

    test_skipped_integration, integrations, is_nightly_integration = collect_integrations(
        integrations_conf, skipped_integration, skipped_integrations_conf, nightly_integrations)

    if playbook_id in filtered_tests:
        playbook_skipped_integration.update(test_skipped_integration)

    skip_nightly_test = (nightly_test or is_nightly_integration) and not is_nightly

    # Skip nightly test
    if skip_nightly_test:
        prints_manager.add_print_job(f'\n------ Test {test_message} start ------', print, thread_index,
                                     include_timestamp=True)
        prints_manager.add_print_job('Skip test', print, thread_index)
        prints_manager.add_print_job(f'------ Test {test_message} end ------\n', print, thread_index,
                                     include_timestamp=True)
        return

    if not run_all_tests:
        # Skip filtered test
        if is_filter_configured and playbook_id not in filtered_tests:
            return

    # Skip bad test
    if playbook_id in skipped_tests_conf:
        skipped_tests.add(f'{playbook_id} - reason: {skipped_tests_conf[playbook_id]}')
        return

    # Skip integration
    if test_skipped_integration:
        return

    # Skip version mismatch test
    test_from_version = t.get('fromversion', '0.0.0')
    test_to_version = t.get('toversion', '99.99.99')

    if not (LooseVersion(test_from_version) <= LooseVersion(server_numeric_version) <= LooseVersion(test_to_version)):
        prints_manager.add_print_job(f'\n------ Test {test_message} start ------', print, thread_index,
                                     include_timestamp=True)
        warning_message = 'Test {} ignored due to version mismatch (test versions: {}-{})'.format(test_message,
                                                                                                  test_from_version,
                                                                                                  test_to_version)
        prints_manager.add_print_job(warning_message, print_warning, thread_index)
        prints_manager.add_print_job(f'------ Test {test_message} end ------\n', print, thread_index,
                                     include_timestamp=True)
        return

    placeholders_map = {'%%SERVER_HOST%%': server}
    are_params_set = set_integration_params(demisto_api_key, integrations, secret_params, instance_names_conf,
                                            playbook_id, prints_manager, placeholders_map, thread_index=thread_index)
    if not are_params_set:
        failed_playbooks.append(playbook_id)
        return

    test_message = update_test_msg(integrations, test_message)
    options = options_handler()
    stdout, stderr = get_docker_memory_data()
    text = 'Memory Usage: {}'.format(stdout) if not stderr else stderr
    if options.nightly and options.memCheck and not tests_settings.is_local_run:
        send_slack_message(slack, SLACK_MEM_CHANNEL_ID, text, 'Content CircleCI', 'False')
        stdout, stderr = get_docker_processes_data()
        text = stdout if not stderr else stderr
        send_slack_message(slack, SLACK_MEM_CHANNEL_ID, text, 'Content CircleCI', 'False')

    run_test(t, tests_queue, tests_settings, demisto_user, demisto_pass, proxy, failed_playbooks,
             integrations, unmockable_integrations, playbook_id, succeed_playbooks, test_message,
             test_options, slack, circle_ci, build_number, server, build_name, prints_manager,
             is_ami, thread_index=thread_index, is_private=is_private)


def get_server_numeric_version(ami_env, is_local_run=False):
    """
    Gets the current server version
    Arguments:
        ami_env: (str)
            AMI version name.
        is_local_run: (bool)
            when running locally, assume latest version.

    Returns:
        (str) Server numeric version
    """
    default_version = '99.99.98'
    env_results_path = './env_results.json'
    if is_local_run:
        print_color(f'Local run, assuming server version is {default_version}', LOG_COLORS.GREEN)
        return default_version

    if not os.path.isfile(env_results_path):
        print_warning(f'Did not find {env_results_path} file, assuming server version is {default_version}.')
        return default_version

    with open(env_results_path, 'r') as json_file:
        env_results = json.load(json_file)

    instances_ami_names = set([env.get('AmiName') for env in env_results if ami_env in env.get('Role', '')])
    if len(instances_ami_names) != 1:
        print_warning(f'Did not get one AMI Name, got {instances_ami_names}.'
                      f' Assuming server version is {default_version}')
        return default_version

    instances_ami_name = list(instances_ami_names)[0]
    extracted_version = re.findall(r'Demisto-(?:Circle-CI|MarketPlace)-Content-[\w-]+-([\d.]+)-[\d]{5}',
                                   instances_ami_name)
    if extracted_version:
        server_numeric_version = extracted_version[0]
    else:
        server_numeric_version = default_version

    # make sure version is three-part version
    if server_numeric_version.count('.') == 1:
        server_numeric_version += ".0"

    print_color(f'Server version: {server_numeric_version}', LOG_COLORS.GREEN)
    return server_numeric_version


def get_instances_ips_and_names(tests_settings):
    if tests_settings.server:
        return [tests_settings.server]
    with open('./Tests/instance_ips.txt', 'r') as instance_file:
        instance_ips = instance_file.readlines()
        instance_ips = [line.strip('\n').split(":") for line in instance_ips]
        return instance_ips


def get_test_records_of_given_test_names(tests_settings, tests_names_to_search):
    conf, secret_conf = load_conf_files(tests_settings.conf_path, tests_settings.secret_conf_path)
    tests_records = conf['tests']
    test_records_with_supplied_names = []
    for test_record in tests_records:
        test_name = test_record.get("playbookID")
        if test_name and test_name in tests_names_to_search:
            test_records_with_supplied_names.append(test_record)
    return test_records_with_supplied_names


def get_json_file(path):
    with open(path, 'r') as json_file:
        return json.loads(json_file.read())


def execute_testing(tests_settings, server_ip, mockable_tests_names, unmockable_tests_names,
                    tests_data_keeper, prints_manager, thread_index=0, is_ami=True):
    server = SERVER_URL.format(server_ip)
    server_numeric_version = tests_settings.serverNumericVersion
    start_message = "Executing tests with the server {} - and the server ip {}".format(server, server_ip)
    prints_manager.add_print_job(start_message, print, thread_index)
    is_nightly = tests_settings.nightly
    is_memory_check = tests_settings.memCheck
    slack = tests_settings.slack
    circle_ci = tests_settings.circleci
    build_number = tests_settings.buildNumber
    build_name = tests_settings.buildName
    is_private = tests_settings.is_private
    conf, secret_conf = load_conf_files(tests_settings.conf_path, tests_settings.secret_conf_path)
    demisto_api_key = tests_settings.api_key
    demisto_user = secret_conf['username']
    demisto_pass = secret_conf['userPassword']

    default_test_timeout = conf.get('testTimeout', 30)

    tests = conf['tests']
    skipped_tests_conf = conf['skipped_tests']
    nightly_integrations = conf['nightly_integrations']
    skipped_integrations_conf = conf['skipped_integrations']
    unmockable_integrations = conf['unmockable_integrations']

    secret_params = secret_conf['integrations'] if secret_conf else []

    filtered_tests, is_filter_configured, run_all_tests = extract_filtered_tests(tests_settings.nightly)
    if is_filter_configured and not run_all_tests:
        is_nightly = True

    if not tests or len(tests) == 0:
        prints_manager.add_print_job('no integrations are configured for test', print, thread_index)
        prints_manager.execute_thread_prints(thread_index)
        return
    xsoar_client = demisto_client.configure(base_url=server, username=demisto_user,
                                            password=demisto_pass, verify_ssl=False)

    # turn off telemetry
    turn_off_telemetry(xsoar_client)

    proxy = None
    if is_ami and not is_private:
        ami = AMIConnection(server_ip)
        ami.clone_mock_data()
        proxy = MITMProxy(server_ip)

    failed_playbooks = []
    succeed_playbooks = []
    skipped_tests = set([])
    skipped_integration = set([])
    playbook_skipped_integration = set([])

    disable_all_integrations(xsoar_client, prints_manager, thread_index=thread_index)
    prints_manager.execute_thread_prints(thread_index)
    mockable_tests = get_test_records_of_given_test_names(tests_settings, mockable_tests_names)
    unmockable_tests = get_test_records_of_given_test_names(tests_settings, unmockable_tests_names)
    if is_private:
        unmockable_tests.extend(mockable_tests)
        mockable_tests = []

    if is_nightly and is_memory_check:
        mem_lim, err = get_docker_limit()
        send_slack_message(slack, SLACK_MEM_CHANNEL_ID,
                           f'Build Number: {build_number}\n Server Address: {server}\nMemory Limit: {mem_lim}',
                           'Content CircleCI', 'False')

    try:
        # first run the mock tests to avoid mockless side effects in container
        if is_ami and mockable_tests and not is_private:
            proxy.configure_proxy_in_demisto(proxy=proxy.ami.docker_ip + ':' + proxy.PROXY_PORT,
                                             username=demisto_user, password=demisto_pass,
                                             server=server)
            executed_in_current_round, mockable_tests_queue = initialize_queue_and_executed_tests_set(mockable_tests)
            while not mockable_tests_queue.empty():
                t = mockable_tests_queue.get()
                executed_in_current_round = update_round_set_and_sleep_if_round_completed(executed_in_current_round,
                                                                                          prints_manager,
                                                                                          t,
                                                                                          thread_index,
                                                                                          mockable_tests_queue)
                run_test_scenario(mockable_tests_queue, tests_settings, t, proxy, default_test_timeout, skipped_tests_conf,
                                  nightly_integrations, skipped_integrations_conf, skipped_integration, is_nightly,
                                  run_all_tests, is_filter_configured, filtered_tests,
                                  skipped_tests, secret_params, failed_playbooks, playbook_skipped_integration,
                                  unmockable_integrations, succeed_playbooks, slack, circle_ci, build_number, server,
                                  build_name, server_numeric_version, demisto_user, demisto_pass,
                                  demisto_api_key, prints_manager, thread_index=thread_index, is_private=is_private)
            proxy.configure_proxy_in_demisto(username=demisto_user, password=demisto_pass, server=server)

            # reset containers after clearing the proxy server configuration
            reset_containers(server, demisto_user, demisto_pass, prints_manager, thread_index)

        prints_manager.add_print_job("\nRunning mock-disabled tests", print, thread_index)
        executed_in_current_round, unmockable_tests_queue = initialize_queue_and_executed_tests_set(unmockable_tests)
        while not unmockable_tests_queue.empty():
            t = unmockable_tests_queue.get()
            executed_in_current_round = update_round_set_and_sleep_if_round_completed(executed_in_current_round,
                                                                                      prints_manager,
                                                                                      t,
                                                                                      thread_index,
                                                                                      unmockable_tests_queue)
            run_test_scenario(unmockable_tests_queue, tests_settings, t, proxy, default_test_timeout,
                              skipped_tests_conf, nightly_integrations, skipped_integrations_conf, skipped_integration,
                              is_nightly, run_all_tests, is_filter_configured, filtered_tests, skipped_tests,
                              secret_params, failed_playbooks, playbook_skipped_integration, unmockable_integrations,
                              succeed_playbooks, slack, circle_ci, build_number, server, build_name,
                              server_numeric_version, demisto_user, demisto_pass, demisto_api_key,
                              prints_manager, thread_index, is_ami, is_private=is_private)
            prints_manager.execute_thread_prints(thread_index)

    except Exception as exc:
        if exc.__class__ == ApiException:
            error_message = exc.body
        else:
            error_message = f'~~ Thread {thread_index + 1} failed ~~\n{str(exc)}\n{traceback.format_exc()}'
        prints_manager.add_print_job(error_message, print_error, thread_index)
        prints_manager.execute_thread_prints(thread_index)
        failed_playbooks.append(f'~~ Thread {thread_index + 1} failed ~~')
        raise

    finally:
        tests_data_keeper.add_tests_data(succeed_playbooks, failed_playbooks, skipped_tests,
                                         skipped_integration, unmockable_integrations)
        if is_ami and not is_private:
            tests_data_keeper.add_proxy_related_test_data(proxy)

            if build_name == 'master':
                updating_mocks_msg = "Pushing new/updated mock files to mock git repo."
                prints_manager.add_print_job(updating_mocks_msg, print, thread_index)
                ami.upload_mock_files(build_name, build_number)

        if playbook_skipped_integration and build_name == 'master':
            comment = 'The following integrations are skipped and critical for the test:\n {}'. \
                format('\n- '.join(playbook_skipped_integration))
            add_pr_comment(comment)


def update_round_set_and_sleep_if_round_completed(executed_in_current_round: set,
                                                  prints_manager: ParallelPrintsManager,
                                                  t: dict,
                                                  thread_index: int,
                                                  unmockable_tests_queue: Queue) -> set:
    """
    Checks if the string representation of the current test configuration is already in
    the executed_in_current_round set.
    If it is- it means we have already executed this test and the we have reached a round and there are tests that
    were not able to be locked by this execution..
    In that case we want to start a new round monitoring by emptying the 'executed_in_current_round' set and sleep
    in order to let the tests be unlocked
    Args:
        executed_in_current_round: A set containing the string representation of all tests configuration as they appear
        in conf.json file that were already executed in the current round
        prints_manager: ParallelPrintsManager object
        t: test configuration as it appears in conf.json file
        thread_index: Currently executing thread
        unmockable_tests_queue: The queue of remaining tests

    Returns:
        A new executed_in_current_round set which contains only the current tests configuration if a round was completed
        else it just adds the new test to the set.
    """
    if str(t) in executed_in_current_round:
        prints_manager.add_print_job(
            'all tests in the queue were executed, sleeping for 30 seconds to let locked tests get unlocked.',
            print,
            thread_index)
        executed_in_current_round = set()
        time.sleep(30)
    executed_in_current_round.add(str(t))
    return executed_in_current_round


def initialize_queue_and_executed_tests_set(tests):
    tests_queue = Queue()
    already_executed_test_playbooks = set()
    for t in tests:
        tests_queue.put(t)
    return already_executed_test_playbooks, tests_queue


def get_unmockable_tests(tests_settings):
    conf, _ = load_conf_files(tests_settings.conf_path, tests_settings.secret_conf_path)
    unmockable_integrations = conf['unmockable_integrations']
    tests = conf['tests']
    unmockable_tests = []
    for test_record in tests:
        test_name = test_record.get("playbookID")
        integrations_used_in_test = get_used_integrations(test_record)
        unmockable_integrations_used = [integration_name for integration_name in integrations_used_in_test if
                                        integration_name in unmockable_integrations]
        if test_name and (not integrations_used_in_test or unmockable_integrations_used):
            unmockable_tests.append(test_name)
    return unmockable_tests


def get_all_tests(tests_settings):
    conf, _ = load_conf_files(tests_settings.conf_path, tests_settings.secret_conf_path)
    tests_records = conf['tests']
    all_tests = []
    for test_record in tests_records:
        test_name = test_record.get("playbookID")
        if test_name:
            all_tests.append(test_name)
    return all_tests


def manage_tests(tests_settings):
    """
    This function manages the execution of Demisto's tests.

    Args:
        tests_settings (TestsSettings): An object containing all the relevant data regarding how the tests should be ran

    """
    tests_settings.serverNumericVersion = get_server_numeric_version(tests_settings.serverVersion,
                                                                     tests_settings.is_local_run)
    instances_ips = get_instances_ips_and_names(tests_settings)
    is_nightly = tests_settings.nightly
    number_of_instances = len(instances_ips)
    prints_manager = ParallelPrintsManager(number_of_instances)
    tests_data_keeper = TestsDataKeeper()

    if tests_settings.server:
        # If the user supplied a server - all tests will be done on that server.
        server_ip = tests_settings.server
        print_color("Starting tests for {}".format(server_ip), LOG_COLORS.GREEN)
        print("Starts tests with server url - https://{}".format(server_ip))
        all_tests = get_all_tests(tests_settings)
        mockable_tests = []
        print(tests_settings.specific_tests_to_run)
        unmockable_tests = tests_settings.specific_tests_to_run if tests_settings.specific_tests_to_run else all_tests
        execute_testing(tests_settings, server_ip, mockable_tests, unmockable_tests, tests_data_keeper, prints_manager,
                        thread_index=0, is_ami=False)

    elif tests_settings.isAMI:
        # Running tests in AMI configuration.
        # This is the way we run most tests, including running Circle for PRs and nightly.
        if is_nightly:
            # If the build is a nightly build, run tests in parallel.
            test_allocation = get_tests_allocation_for_threads(number_of_instances, tests_settings.conf_path)
            current_thread_index = 0
            all_unmockable_tests_list = get_unmockable_tests(tests_settings)
            threads_array = []

            for ami_instance_name, ami_instance_ip in instances_ips:
                if ami_instance_name == tests_settings.serverVersion:  # Only run tests for given AMI Role
                    current_instance = ami_instance_ip
                    tests_allocation_for_instance = test_allocation[current_thread_index]

                    unmockable_tests = [test for test in all_unmockable_tests_list
                                        if test in tests_allocation_for_instance]
                    mockable_tests = [test for test in tests_allocation_for_instance if test not in unmockable_tests]
                    print_color("Starting tests for {}".format(ami_instance_name), LOG_COLORS.GREEN)
                    print("Starts tests with server url - https://{}".format(ami_instance_ip))

                    if number_of_instances == 1:
                        execute_testing(tests_settings, current_instance, mockable_tests, unmockable_tests,
                                        tests_data_keeper, prints_manager, thread_index=0, is_ami=True)
                    else:
                        thread_kwargs = {
                            "tests_settings": tests_settings,
                            "server_ip": current_instance,
                            "mockable_tests_names": mockable_tests,
                            "unmockable_tests_names": unmockable_tests,
                            "thread_index": current_thread_index,
                            "prints_manager": prints_manager,
                            "tests_data_keeper": tests_data_keeper,
                        }
                        t = threading.Thread(target=execute_testing, kwargs=thread_kwargs)
                        threads_array.append(t)
                        t.start()
                        current_thread_index += 1

            for t in threads_array:
                t.join()

        else:
            for ami_instance_name, ami_instance_ip in instances_ips:
                if ami_instance_name == tests_settings.serverVersion:
                    print_color("Starting tests for {}".format(ami_instance_name), LOG_COLORS.GREEN)
                    print("Starts tests with server url - https://{}".format(ami_instance_ip))
                    all_tests = get_all_tests(tests_settings)
                    unmockable_tests = get_unmockable_tests(tests_settings)
                    mockable_tests = [test for test in all_tests if test not in unmockable_tests]
                    execute_testing(tests_settings, ami_instance_ip, mockable_tests, unmockable_tests,
                                    tests_data_keeper, prints_manager, thread_index=0, is_ami=True)
                    sleep(8)

    else:
        # TODO: understand better when this occurs and what will be the settings
        # This case is rare, and usually occurs on two cases:
        # 1. When someone from Server wants to trigger a content build on their branch.
        # 2. When someone from content wants to run tests on a specific build.
        server_numeric_version = '99.99.98'  # assume latest
        print("Using server version: {} (assuming latest for non-ami)".format(server_numeric_version))
        instance_ip = instances_ips[0][1]
        all_tests = get_all_tests(tests_settings)
        execute_testing(tests_settings, instance_ip, [], all_tests,
                        tests_data_keeper, prints_manager, thread_index=0, is_ami=False)

    print_test_summary(tests_data_keeper, tests_settings.isAMI)
    create_result_files(tests_data_keeper)

    if tests_data_keeper.failed_playbooks:
        tests_failed_msg = "Some tests have failed. Not destroying instances."
        print(tests_failed_msg)
        sys.exit(1)


def add_pr_comment(comment):
    token = os.environ['CONTENT_GITHUB_TOKEN']
    branch_name = os.environ['CIRCLE_BRANCH']
    sha1 = os.environ['CIRCLE_SHA1']

    query = '?q={}+repo:demisto/content+org:demisto+is:pr+is:open+head:{}+is:open'.format(sha1, branch_name)
    url = 'https://api.github.com/search/issues'
    headers = {'Authorization': 'Bearer ' + token}
    try:
        res = requests.get(url + query, headers=headers, verify=False)
        res = handle_github_response(res)

        if res and res.get('total_count', 0) == 1:
            issue_url = res['items'][0].get('comments_url') if res.get('items', []) else None
            if issue_url:
                res = requests.post(issue_url, json={'body': comment}, headers=headers, verify=False)
                handle_github_response(res)
        else:
            print_warning('Add pull request comment failed: There is more then one open pull request for branch {}.'
                          .format(branch_name))
    except Exception as e:
        print_warning('Add pull request comment failed: {}'.format(e))


def handle_github_response(response):
    res_dict = response.json()
    if not res_dict.ok:
        print_warning('Add pull request comment failed: {}'.
                      format(res_dict.get('message')))
    return res_dict


@contextmanager
def acquire_test_lock(integrations_details: list,
                      test_timeout: int,
                      prints_manager: ParallelPrintsManager,
                      thread_index: int,
                      test_settings: TestsSettings) -> None:
    """
    This is a context manager that handles all the locking and unlocking of integrations.
    Execution is as following:
    * Attempts to lock the test's integrations and yields the result of this attempt
    * If lock attempt has failed - yields False, if it succeeds - yields True
    * Once the test is done- will unlock all integrations
    Args:
        integrations_details: test integrations details
        test_timeout: test timeout in seconds
        prints_manager: ParallelPrintsManager object
        thread_index: The index of the thread that executes the unlocking
        conf_json_path: Path to conf.json file
    Yields:
        A boolean indicating the lock attempt result
    """
    locked = safe_lock_integrations(test_timeout,
                                    prints_manager,
                                    integrations_details,
                                    thread_index,
                                    test_settings)
    try:
        yield locked
    finally:
        if not locked:
            return
        safe_unlock_integrations(prints_manager, integrations_details, thread_index, test_settings)
        prints_manager.execute_thread_prints(thread_index)


def safe_unlock_integrations(prints_manager: ParallelPrintsManager, integrations_details: list, thread_index: int, test_settings: TestsSettings):
    """
    This integration safely unlocks the test's integrations.
    If an unexpected error occurs - this method will log it's details and other tests execution will continue
    Args:
        prints_manager: ParallelPrintsManager object
        integrations_details: Details of the currently executed test
        thread_index: The index of the thread that executes the unlocking
    """
    try:
        # executing the test could take a while, re-instancing the storage client
        storage_client = init_storage_client(test_settings.service_account)
        unlock_integrations(integrations_details, prints_manager, storage_client, thread_index)
    except Exception as e:
        prints_manager.add_print_job(f'attempt to unlock integration failed for unknown reason.\nError: {e}',
                                     print_warning,
                                     thread_index,
                                     include_timestamp=True)


def safe_lock_integrations(test_timeout: int,
                           prints_manager: ParallelPrintsManager,
                           integrations_details: list,
                           thread_index: int,
                           test_settings: TestsSettings) -> bool:
    """
    This integration safely locks the test's integrations and return it's result
    If an unexpected error occurs - this method will log it's details and return False
    Args:
        test_timeout: Test timeout in seconds
        prints_manager: ParallelPrintsManager object
        integrations_details: test integrations details
        thread_index: The index of the thread that executes the unlocking
        test_settings: Path to conf.json file

    Returns:
        A boolean indicating the lock attempt result
    """
    conf, _ = load_conf_files(test_settings.conf_path, None)
    parallel_integrations_names = conf['parallel_integrations']
    filtered_integrations_details = [integration for integration in integrations_details if
                                     integration['name'] not in parallel_integrations_names]
    integration_names = get_integrations_list(filtered_integrations_details)
    if integration_names:
        print_msg = f'Attempting to lock integrations {integration_names}, with timeout {test_timeout}'
    else:
        print_msg = 'No integrations to lock'
    prints_manager.add_print_job(print_msg, print, thread_index, include_timestamp=True)
    try:
        storage_client = init_storage_client(test_settings.service_account)
        locked = lock_integrations(filtered_integrations_details, test_timeout, storage_client, prints_manager, thread_index)
    except Exception as e:
        prints_manager.add_print_job(f'attempt to lock integration failed for unknown reason.\nError: {e}',
                                     print_warning,
                                     thread_index,
                                     include_timestamp=True)
        locked = False
    return locked


def workflow_still_running(workflow_id: str) -> bool:
    """
    This method takes a workflow id and checks if the workflow is still running
    If given workflow ID is the same as the current workflow, will simply return True
    else it will query circleci api for the workflow and return the status
    Args:
        workflow_id: The ID of the workflow

    Returns:
        True if the workflow is running, else False
    """
    # If this is the current workflow_id
    if workflow_id == WORKFLOW_ID:
        return True
    else:
        try:
            workflow_details_response = requests.get(f'https://circleci.com/api/v2/workflow/{workflow_id}',
                                                     headers={'Accept': 'application/json'},
                                                     auth=(CIRCLE_STATUS_TOKEN, ''))
            workflow_details_response.raise_for_status()
        except Exception as e:
            print(f'Failed to get circleci response about workflow with id {workflow_id}, error is: {e}')
            return True
        return workflow_details_response.json().get('status') not in ('canceled', 'success', 'failed')


def lock_integrations(integrations_details: list,
                      test_timeout: int,
                      storage_client: storage.Client,
                      prints_manager: ParallelPrintsManager,
                      thread_index: int) -> bool:
    """
    Locks all the test's integrations
    Args:
        integrations_details: List of current test's integrations
        test_timeout: Test timeout in seconds
        storage_client: The GCP storage client
        prints_manager: ParallelPrintsManager object
        thread_index: The index of the thread that executes the unlocking

    Returns:
        True if all the test's integrations were successfully locked, else False
    """
    integrations = get_integrations_list(integrations_details)
    if not integrations:
        return True
    existing_integrations_lock_files = get_locked_integrations(integrations, storage_client)
    for integration, lock_file in existing_integrations_lock_files.items():
        # Each file has content in the form of <circleci-build-number>:<timeout in seconds>
        # If it has not expired - it means the integration is currently locked by another test.
        workflow_id, build_number, lock_timeout = lock_file.download_as_string().decode().split(':')
        if not lock_expired(lock_file, lock_timeout) and workflow_still_running(workflow_id):
            # there is a locked integration for which the lock is not expired - test cannot be executed at the moment
            prints_manager.add_print_job(
                f'Could not lock integration {integration}, another lock file was exist with '
                f'build number: {build_number}, timeout: {lock_timeout}, last update at {lock_file.updated}.\n'
                f'Delaying test execution',
                print,
                thread_index,
                include_timestamp=True)
            return False
    integrations_generation_number = {}
    # Gathering generation number with which the new file will be created,
    # See https://cloud.google.com/storage/docs/generations-preconditions for details.
    for integration in integrations:
        if integration in existing_integrations_lock_files:
            integrations_generation_number[integration] = existing_integrations_lock_files[integration].generation
        else:
            integrations_generation_number[integration] = 0
    return create_lock_files(integrations_generation_number, prints_manager,
                             storage_client, integrations_details, test_timeout, thread_index)


def get_integrations_list(test_integrations: list) -> list:
    """
    Since test details can have one integration as a string and sometimes a list of integrations- this methods
    parses the test's integrations into a list of integration names.
    Args:
        test_integrations: List of current test's integrations
    Returns:
        the integration names in a list for all the integrations that takes place in the test
        specified in test details.
    """
    return [integration['name'] for integration in test_integrations]


def create_lock_files(integrations_generation_number: dict,
                      prints_manager: ParallelPrintsManager,
                      storage_client: storage.Client,
                      integrations_details: list,
                      test_timeout: int,
                      thread_index: int) -> bool:
    """
    This method tries to create a lock files for all integrations specified in 'integrations_generation_number'.
    Each file should contain <circle-ci-build-number>:<test-timeout>
    where the <circle-ci-build-number> part is for debugging and troubleshooting
    and the <test-timeout> part is to be able to unlock revoked test files.
    If for any of the integrations, the lock file creation will fail- the already created files will be cleaned.
    Args:
        integrations_generation_number: A dict in the form of {<integration-name>:<integration-generation>}
        prints_manager: ParallelPrintsManager object
        storage_client: The GCP storage client
        integrations_details: List of current test's integrations
        test_timeout: The time out
        thread_index:

    Returns:

    """
    locked_integrations = []
    bucket = storage_client.bucket(BUCKET_NAME)
    for integration, generation_number in integrations_generation_number.items():
        blob = bucket.blob(f'{LOCKS_PATH}/{integration}')
        try:
            blob.upload_from_string(f'{WORKFLOW_ID}:{CIRCLE_BUILD_NUM}:{test_timeout + 30}',
                                    if_generation_match=generation_number)
            prints_manager.add_print_job(f'integration {integration} locked',
                                         print,
                                         thread_index,
                                         include_timestamp=True)
            locked_integrations.append(integration)
        except PreconditionFailed:
            # if this exception occurs it means that another build has locked this integration
            # before this build managed to do it.
            # we need to unlock all the integrations we have already locked and try again later
            prints_manager.add_print_job(
                f'Could not lock integration {integration}, Create file with precondition failed.'
                f'delaying test execution.',
                print_warning,
                thread_index,
                include_timestamp=True)
            unlock_integrations(integrations_details, prints_manager, storage_client, thread_index)
            return False
    return True


def unlock_integrations(integrations_details: list,
                        prints_manager: ParallelPrintsManager,
                        storage_client: storage.Client,
                        thread_index: int) -> None:
    """
    Delete all integration lock files for integrations specified in 'locked_integrations'
    Args:
        integrations_details: List of current test's integrations
        prints_manager: ParallelPrintsManager object
        storage_client: The GCP storage client
        thread_index: The index of the thread that executes the unlocking
    """
    locked_integrations = get_integrations_list(integrations_details)
    locked_integration_blobs = get_locked_integrations(locked_integrations, storage_client)
    for integration, lock_file in locked_integration_blobs.items():
        try:
            # Verifying build number is the same as current build number to avoid deleting other tests lock files
            _, build_number, _ = lock_file.download_as_string().decode().split(':')
            if build_number == CIRCLE_BUILD_NUM:
                lock_file.delete(if_generation_match=lock_file.generation)
                prints_manager.add_print_job(
                    f'Integration {integration} unlocked',
                    print,
                    thread_index,
                    include_timestamp=True)
        except PreconditionFailed:
            prints_manager.add_print_job(f'Could not unlock integration {integration} precondition failure',
                                         print_warning,
                                         thread_index,
                                         include_timestamp=True)


def get_locked_integrations(integrations: list, storage_client: storage.Client) -> dict:
    """
    Getting all locked integrations files
    Args:
        integrations: Integrations that we want to get lock files for
        storage_client: The GCP storage client

    Returns:
        A dict of the form {<integration-name>:<integration-blob-object>} for all integrations that has a blob object.
    """
    # Listing all files in lock folder
    # Wrapping in 'list' operator because list_blobs return a generator which can only be iterated once
    lock_files_ls = list(storage_client.list_blobs(BUCKET_NAME, prefix=f'{LOCKS_PATH}'))
    current_integrations_lock_files = {}
    # Getting all existing files details for integrations that we want to lock
    for integration in integrations:
        current_integrations_lock_files.update({integration: [lock_file_blob for lock_file_blob in lock_files_ls if
                                                              lock_file_blob.name == f'{LOCKS_PATH}/{integration}']})
    # Filtering 'current_integrations_lock_files' from integrations with no files
    current_integrations_lock_files = {integration: blob_files[0] for integration, blob_files in
                                       current_integrations_lock_files.items() if blob_files}
    return current_integrations_lock_files


def lock_expired(lock_file: storage.Blob, lock_timeout: str) -> bool:
    """
    Checks if the time that passed since the creation of the 'lock_file' is more then 'lock_timeout'.
    If not- it means that the integration represented by the lock file is currently locked and is tested in another build
    Args:
        lock_file: The lock file blob object
        lock_timeout: The expiration timeout of the lock in seconds

    Returns:
        True if the lock has expired it's timeout, else False
    """
    return datetime.datetime.now(tz=pytz.utc) - lock_file.updated >= datetime.timedelta(seconds=int(lock_timeout))


def main():
    print("Time is: {}\n\n\n".format(datetime.datetime.now()))
    tests_settings = options_handler()

    # should be removed after solving: https://github.com/demisto/etc/issues/21383
    # -------------
    if 'master' in tests_settings.serverVersion.lower():
        print('[{}] sleeping for 30 secs'.format(datetime.datetime.now()))
        sleep(45)
    # -------------
    manage_tests(tests_settings)


if __name__ == '__main__':
    main()
