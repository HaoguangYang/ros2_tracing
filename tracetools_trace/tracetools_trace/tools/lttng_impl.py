# Copyright 2019 Robert Bosch GmbH
# Copyright 2021 Christophe Bedard
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Implementation of the interface for tracing with LTTng."""

import os
import re
import shlex
import subprocess
from typing import Dict
from typing import List
from typing import Optional
from typing import Set
from typing import Tuple
from typing import Union

import lttng
from packaging.version import Version

from .names import CONTEXT_TYPE_CONSTANTS_MAP
from .names import DEFAULT_CONTEXT
from .names import DEFAULT_EVENTS_ROS


def get_version() -> Optional[Version]:
    """
    Get the version of the lttng module.

    The module does not have a __version__ attribute, but the version is mentioned in its __doc__,
    and seems to be written in a consistent way across versions.

    :return: the version as a Version object, or `None` if it cannot be extracted
    """
    doc_lines = str(lttng.__doc__).split('\n')
    filtered_doc_lines: List[str] = list(filter(None, doc_lines))
    if len(filtered_doc_lines) == 0:
        return None
    first_line = filtered_doc_lines[0]
    version_string = first_line.split(' ')[1]
    if not re.compile(r'^[0-9]+\.[0-9]+\.[0-9]+$').match(version_string):
        return None
    return Version(version_string)


def is_kernel_tracer_available() -> Tuple[bool, Optional[str]]:
    """
    Check if the kernel tracer is available.

    Runs 'lttng list -k', which gives an error if the kernel tracer is not available.

    :return: (`True` if available or `False` if not, stderr output if unavailable)
    """
    process = subprocess.run(
        ['lttng', 'list', '-k'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if 0 != process.returncode:
        return False, process.stderr.decode().strip('\n')
    return True, None


def get_lttng_home() -> Optional[str]:
    """
    Get the LTTng home value.

    $LTTNG_HOME, or $HOME if unset: https://lttng.org/man/1/lttng/v2.13/#doc-_files

    :return: the LTTng home value
    """
    return os.environ.get('LTTNG_HOME') or os.environ.get('HOME')


def get_session_daemon_pid() -> Optional[int]:
    """
    Get the non-root session daemon PID, if there is one.

    This does not apply to root session daemons.

    :return: the non-root session daemon PID, or `None` if there is none
    """
    # When a non-root session daemon is started, its PID is written to .lttng/lttng-sessiond.pid
    # under the LTTng home
    lttng_home = get_lttng_home()
    # If we can't find the home, then we can just assume that there is no session daemon
    if not lttng_home:
        return None
    # If the file doesn't exist, there is no session daemon
    lttng_sessiond_pid = os.path.join(lttng_home, '.lttng', 'lttng-sessiond.pid')
    if not os.path.isfile(lttng_sessiond_pid):
        return None
    with open(lttng_sessiond_pid, 'r') as f:
        pid = f.read().strip()
        if not pid.isdigit():
            return None
        return int(pid)


def is_session_daemon_unreachable() -> bool:
    """
    Check if the session daemon appears to exist while being unreachable.

    This tries to detect cases of this LTTng issue: https://bugs.lttng.org/issues/1371
    If this issue happens, LTTng will think that the session daemon exists and will happily trace,
    but it will silently not record any trace data, since there is no actual session daemon.
    Therefore, if this returns `True`, then tracing will silently not work.

    TODO(christophebedard) remove this once Rolling uses a version of LTTng with a fix for this bug

    :return: `True` if the session daemon is unreachable, `False` otherwise
    """
    pid = get_session_daemon_pid()
    # If we can't find the PID, then the session daemon really doesn't exist and we can just create
    # one, so it's fine
    if pid is None:
        return False
    # Otherwise, try to look up the process with that PID; if we can't find it, or if it is not
    # lttng-sessiond, then it means that the session daemon is unreachable
    process = subprocess.run(
        shlex.split(f'ps -o comm= {pid}'),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding='utf-8',
    )
    return 1 == process.returncode or 'lttng-sessiond' != process.stdout.strip()


def setup(
    *,
    session_name: str,
    base_path: str,
    append_trace: bool = False,
    ros_events: Union[List[str], Set[str]] = DEFAULT_EVENTS_ROS,
    kernel_events: Union[List[str], Set[str]] = [],
    context_fields: Union[List[str], Set[str], Dict[str, List[str]]] = DEFAULT_CONTEXT,
    channel_name_ust: str = 'ros2',
    channel_name_kernel: str = 'kchan',
    subbuffer_size_ust: int = 8 * 4096,
    subbuffer_size_kernel: int = 32 * 4096,
) -> Optional[str]:
    """
    Set up LTTng session, with events and context.

    See: https://lttng.org/docs/#doc-core-concepts

    Initialization will fail if the list of kernel events to be
    enabled is not empty and if the kernel tracer is not installed.

    Raises RuntimeError on failure, in which case the tracing session might still exist.

    :param session_name: the name of the session
    :param base_path: the path to the directory in which to create the tracing session directory,
        which will be created if needed
    :param append_trace: whether to append to the trace directory if it already exists, otherwise
        an error is reported
    :param ros_events: list of ROS events to enable
    :param kernel_events: list of kernel events to enable
    :param context_fields: the names of context fields to enable
        if it's a list or a set, the context fields are enabled for both kernel and userspace;
        if it's a dictionary: { domain type string -> context fields list }
            with the domain type string being either 'kernel' or 'userspace'
    :param channel_name_ust: the UST channel name
    :param channel_name_kernel: the kernel channel name
    :param subbuffer_size_ust: the size of the subbuffers for userspace events (defaults to 8 times
        the usual page size)
    :param subbuffer_size_kernel: the size of the subbuffers for kernel events (defaults to 32
        times the usual page size, since there can be way more kernel events than UST events)
    :return: the full path to the trace directory, or `None` if initialization failed
    """
    # Validate parameters
    if not session_name:
        raise RuntimeError('empty session name')
    # Resolve full tracing directory path
    full_path = os.path.join(base_path, session_name)
    if os.path.isdir(full_path) and not append_trace:
        raise RuntimeError(
            f'trace directory already exists, use the append option to append to it: {full_path}')

    # If there is no session daemon running, try to spawn one
    if lttng.session_daemon_alive() == 0:
        subprocess.run(
            ['lttng-sessiond', '--daemonize'],
        )
    # Error out if it looks like there is a session daemon that we can't actually reach
    if is_session_daemon_unreachable():
        raise RuntimeError(
            'lttng-sessiond seems to exist, but is unreachable. '
            'If using two containers with the same HOME directory, set the LTTNG_HOME environment '
            'variable to the path to a unique directory for each container and make sure that the '
            'directory exists. See: https://bugs.lttng.org/issues/1371'
        )
    # Error out if there is still no session daemon
    if lttng.session_daemon_alive() == 0:
        raise RuntimeError('failed to start lttng session daemon')

    # Make sure the kernel tracer is available if there are kernel events
    # Do this after spawning a session daemon, otherwise we can't detect the kernel tracer
    if 0 < len(kernel_events):
        kernel_tracer_available, message = is_kernel_tracer_available()
        if not kernel_tracer_available:
            raise RuntimeError(
                f'kernel tracer is not available: {message}\n'
                '  cannot use kernel events:\n'
                "    'ros2 trace' command: cannot use '-k' option\n"
                "    'Trace' action: cannot set 'events_kernel'/'events-kernel' list\n"
                '  install the kernel tracer, e.g., on Ubuntu, install lttng-modules-dkms\n'
                '  see: https://github.com/ros2/ros2_tracing#building'
            )

    # Convert lists to sets
    if not isinstance(ros_events, set):
        ros_events = set(ros_events)
    if not isinstance(kernel_events, set):
        kernel_events = set(kernel_events)
    if isinstance(context_fields, list):
        context_fields = set(context_fields)

    ust_enabled = ros_events is not None and len(ros_events) > 0
    kernel_enabled = kernel_events is not None and len(kernel_events) > 0

    # Domains
    if ust_enabled:
        domain_ust = lttng.Domain()
        domain_ust.type = lttng.DOMAIN_UST
        # Per-user buffer
        domain_ust.buf_type = lttng.BUFFER_PER_UID
        channel_ust = lttng.Channel()
        channel_ust.name = channel_name_ust
        # Discard, do not overwrite
        channel_ust.attr.overwrite = 0
        # We use 2 sub-buffers because the number of sub-buffers is pointless in discard mode,
        # and switching between sub-buffers introduces noticeable CPU overhead
        channel_ust.attr.subbuf_size = subbuffer_size_ust
        channel_ust.attr.num_subbuf = 2
        # Ignore switch timer interval and use read timer instead
        channel_ust.attr.switch_timer_interval = 0
        channel_ust.attr.read_timer_interval = 200
        # mmap channel output (only option for UST)
        channel_ust.attr.output = lttng.EVENT_MMAP
        events_list_ust = _create_events(ros_events)
    if kernel_enabled:
        domain_kernel = lttng.Domain()
        domain_kernel.type = lttng.DOMAIN_KERNEL
        # Global buffer (only option for kernel domain)
        domain_kernel.buf_type = lttng.BUFFER_GLOBAL
        channel_kernel = lttng.Channel()
        channel_kernel.name = channel_name_kernel
        # Discard, do not overwrite
        channel_kernel.attr.overwrite = 0
        channel_kernel.attr.subbuf_size = subbuffer_size_kernel
        channel_kernel.attr.num_subbuf = 2
        # Ignore switch timer interval and use read timer instead
        channel_kernel.attr.switch_timer_interval = 0
        channel_kernel.attr.read_timer_interval = 200
        # mmap channel output instead of splice
        channel_kernel.attr.output = lttng.EVENT_MMAP
        events_list_kernel = _create_events(kernel_events)

    # Create session
    # LTTng will create the parent directories if needed
    _create_session(session_name, full_path)

    # Handles, channels, events
    handle_ust = None
    if ust_enabled:
        handle_ust = _create_handle(session_name, domain_ust)
        _enable_channel(handle_ust, channel_ust)
        _enable_events(handle_ust, events_list_ust, channel_ust.name)
    handle_kernel = None
    if kernel_enabled:
        handle_kernel = _create_handle(session_name, domain_kernel)
        _enable_channel(handle_kernel, channel_kernel)
        _enable_events(handle_kernel, events_list_kernel, channel_kernel.name)

    # Context
    contexts_dict = _normalize_contexts_dict(
        {'kernel': handle_kernel, 'userspace': handle_ust}, context_fields)
    _add_context(contexts_dict)

    return full_path


def start(
    *,
    session_name: str,
    **kwargs,
) -> None:
    """
    Start LTTng session, and check for errors.

    Raises RuntimeError on failure to start.

    :param session_name: the name of the session
    """
    result = lttng.start(session_name)
    if result < 0:
        raise RuntimeError(f'failed to start tracing: {lttng.strerror(result)}')


def stop(
    *,
    session_name: str,
    ignore_error: bool = False,
    **kwargs,
) -> None:
    """
    Stop LTTng session, and check for errors.

    Raises RuntimeError on failure to stop, unless ignored.

    :param session_name: the name of the session
    :param ignore_error: whether to ignore any error when stopping
    """
    result = lttng.stop(session_name)
    if result < 0 and not ignore_error:
        raise RuntimeError(f'failed to stop tracing: {lttng.strerror(result)}')


def destroy(
    *,
    session_name: str,
    ignore_error: bool = False,
    **kwargs,
) -> None:
    """
    Destroy LTTng session, and check for errors.

    Raises RuntimeError on failure to stop, unless ignored.

    :param session_name: the name of the session
    :param ignore_error: whether to ignore any error when destroying
    """
    result = lttng.destroy(session_name)
    if result < 0 and not ignore_error:
        raise RuntimeError(f'failed to destroy tracing session: {lttng.strerror(result)}')


def _create_events(
    event_names: Set[str],
) -> List[lttng.Event]:
    """
    Create events list from names.

    :param event_names: a set of names to create events for
    :return: the list of events
    """
    events_list = []
    for event_name in event_names:
        e = lttng.Event()
        e.name = event_name
        e.type = lttng.EVENT_TRACEPOINT
        e.loglevel_type = lttng.EVENT_LOGLEVEL_ALL
        events_list.append(e)
    return events_list


def _create_session(
    session_name: str,
    full_path: str,
) -> None:
    """
    Create session from name and full directory path, and check for errors.

    :param session_name: the name of the session
    :param full_path: the full path to the main directory to write trace data to
    """
    result = lttng.create(session_name, full_path)
    # See lttng-tools/include/lttng/lttng-error.h
    if -28 == result:
        # Sessions seem to persist, so if it already exists,
        # just destroy it and try again
        destroy(session_name=session_name)
        result = lttng.create(session_name, full_path)
    if result < 0:
        raise RuntimeError(f'session creation failed: {lttng.strerror(result)}')


def _create_handle(
    session_name: str,
    domain: lttng.Domain,
) -> lttng.Handle:
    """
    Create a handle for a given session name and a domain, and check for errors.

    :param session_name: the name of the session
    :param domain: the domain to be used
    :return: the handle
    """
    handle = None
    handle = lttng.Handle(session_name, domain)
    if handle is None:
        raise RuntimeError('handle creation failed')
    return handle


def _enable_channel(
    handle: lttng.Handle,
    channel: lttng.Channel,
) -> None:
    """
    Enable channel for a handle, and check for errors.

    :param handle: the handle to be used
    :param channel: the channel to enable
    """
    result = lttng.enable_channel(handle, channel)
    if result < 0:
        raise RuntimeError(f'channel enabling failed: {lttng.strerror(result)}')


def _enable_events(
    handle: lttng.Handle,
    events_list: List[lttng.Event],
    channel_name: str,
) -> None:
    """
    Enable events list for a given handle and channel name, and check for errors.

    :param handle: the handle to be used
    :param events_list: the list of events to enable
    :param channel_name: the name of the channel to associate
    """
    for event in events_list:
        result = lttng.enable_event(handle, event, channel_name)
        if result < 0:
            raise RuntimeError(f'event enabling failed: {lttng.strerror(result)}')


context_map = {
    name: getattr(lttng, name_constant, None) if name_constant is not None else None
    for name, name_constant in CONTEXT_TYPE_CONSTANTS_MAP.items()
}


def _context_field_name_to_type(
    context_field_name: str,
) -> Optional[int]:
    """
    Convert from context name to LTTng enum/constant type.

    :param context_field_name: the generic name for the context field
    :return: the associated type, or `None` if it cannot be found
    """
    return context_map.get(context_field_name, None)


def _create_context_list(
    context_fields: Set[str],
) -> List[lttng.EventContext]:
    """
    Create context list from field names, and check for errors.

    :param context_fields: the set of context fields
    :return: the event context list
    """
    context_list = []
    for context_field_name in context_fields:
        ec = lttng.EventContext()
        context_type = _context_field_name_to_type(context_field_name)
        if context_type is not None:
            ec.ctx = context_type
            context_list.append(ec)
        else:
            raise RuntimeError(f'failed to find context type: {context_field_name}')
    return context_list


def _normalize_contexts_dict(
    handles: Dict[str, Optional[lttng.Handle]],
    context_fields: Union[Set[str], Dict[str, List[str]]],
) -> Dict[lttng.Handle, List[lttng.EventContext]]:
    """
    Normalize context list/set/dict to dict.

    :param handles: the mapping from domain type name to handle
    :param context_fields: the set of context field names,
        or mapping from domain type name to list of context field names
    :return: a dictionary of handle to list of event contexts
    """
    handles = {domain: handle for domain, handle in handles.items() if handle is not None}
    contexts_dict = {}
    if isinstance(context_fields, set):
        contexts_dict = {h: _create_context_list(context_fields) for _, h in handles.items()}
    elif isinstance(context_fields, dict):
        contexts_dict = \
            {h: _create_context_list(set(context_fields[d])) for d, h in handles.items()}
    else:
        assert False
    return contexts_dict


def _add_context(
    contexts: Dict[lttng.Handle, List[lttng.EventContext]],
) -> None:
    """
    Add context lists to given handles, and check for errors.

    :param contexts: the dictionay of context handles -> event contexts
    """
    for handle, contexts_list in contexts.items():
        for context in contexts_list:
            result = lttng.add_context(handle, context, None, None)
            if result < 0:
                raise RuntimeError(
                    f'failed to add context field {str(context)}: {lttng.strerror(result)}')
