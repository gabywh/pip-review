"""

pip-review

pip-review is a convenience wrapper around pip.

Module main.

Responsible for
1. cmdline parsing if called via cmdline.
2. parsing requirements and constraints files for pip.

See the README.rst for the full description.

"""

from __future__ import absolute_import

import argparse
import json
import logging
import re
import subprocess
import sys
from functools import partial
from operator import itemgetter

import pip
from packaging import version


def is_python27():
    return sys.version_info.major == 2 and sys.version_info.minor == 7


def is_python3():
    return sys.version_info.major == 3


if is_python3():

    def check_output(*args, **kwargs):
        with subprocess.Popen(
            stdout=subprocess.PIPE, *args, **kwargs
        ) as process:
            output, _ = process.communicate()
            retcode = process.poll()
            if retcode:
                error = subprocess.CalledProcessError(retcode, args[0])
                error.output = output
                raise error
            return output
else:  # Python2 Imports
    from subprocess import check_output  # noqa: I001

    # when running pylint with python v3, it complains about
    # __builtin__ since it was renamed to __builtins__ in v3
    # but since we know at runtime we are in v2 here, then disable

    # pylint: disable=redefined-builtin, import-error
    import __builtin__

    input = getattr(__builtin__, 'raw_input')
    # pylint: enable=redefined-builtin, import-error


VERSION_PATTERN = re.compile(
    version.VERSION_PATTERN,
    re.VERBOSE | re.IGNORECASE,  # necessary according to the `packaging` docs
)

NAME_PATTERN = re.compile(r'[a-z0-9_-]+', re.IGNORECASE)

EPILOG = """
Unrecognised arguments will be forwarded to pip list --outdated and
pip install, so you can pass things such as --user, --pre and --timeout
and they will do what you expect. See pip list -h and pip install -h
for a full overview of the options.
"""

DEPRECATED_NOTICE = """
Support for Python 2.6 and Python 3.2 has been stopped. From
version 1.0 onwards, pip-review only supports Python==2.7 and
Python>=3.3.
"""

# parameters that pip list supports but not pip install
LIST_ONLY = {
    'l',
    'local',
    'path',
    'format',
    'not-required',
    'exclude-editable',
    'include-editable',
    'exclude',
}

# parameters that pip install supports but not pip list
INSTALL_ONLY = {
    'c',
    'constraint',
    'no-deps',
    't',
    'target',
    'platform',
    'python-version',
    'implementation',
    'abi',
    'root',
    'prefix',
    'b',
    'build',
    'src',
    'U',
    'upgrade',
    'upgrade-strategy',
    'force-reinstall',
    'I',
    'ignore-installed',
    'ignore-requires-python',
    'no-build-isolation',
    'use-pep517',
    'install-option',
    'global-option',
    'compile',
    'no-compile',
    'no-warn-script-location',
    'no-warn-conflicts',
    'no-binary',
    'only-binary',
    'prefer-binary',
    'no-clean',
    'require-hashes',
    'progress-bar',
}

# command that sets up the pip module of the current Python interpreter
PIP_CMD = [sys.executable, '-m', 'pip']

# nicer headings for the columns in the oudated package table
COLUMNS = {
    'Package': 'name',
    'Version': 'version',
    'Latest': 'latest_version',
    'Type': 'latest_filetype',
}

# version-specific information to be add to the help page
VERSION_EPILOG = (
    DEPRECATED_NOTICE if (2, 7) > sys.version_info >= (3, 3) else ''
)


def parse_args():
    description = (
        'Keeps your Python packages fresh. Looking for a new maintainer!\
            See https://github.com/jgonggrijp/pip-review/issues/76'
    )
    parser = argparse.ArgumentParser(
        description=description,
        epilog=EPILOG + VERSION_EPILOG,
    )
    parser.add_argument(
        '--verbose',
        '-v',
        action='store_true',
        default=False,
        help='Show more output',
    )
    parser.add_argument(
        '--raw',
        '-r',
        action='store_true',
        default=False,
        help='Print raw lines (suitable for passing to pip install)',
    )
    parser.add_argument(
        '--interactive',
        '-i',
        action='store_true',
        default=False,
        help='Ask interactively to install updates',
    )
    parser.add_argument(
        '--auto',
        '-a',
        action='store_true',
        default=False,
        help='Automatically install every update found',
    )
    parser.add_argument(
        '--continue-on-fail',
        '-C',
        action='store_true',
        default=False,
        help='Continue with other installs when one fails',
    )
    parser.add_argument(
        '--freeze-outdated-packages',
        action='store_true',
        default=False,
        help='Freeze all outdated packages to "requirements.txt" before upgrading them',
    )
    parser.add_argument(
        '--preview',
        '-p',
        action='store_true',
        default=False,
        help='Preview update target list before execution',
    )
    parser.add_argument(
        '--preview-only',
        '-P',
        action='store_true',
        default=False,
        help='Preview only',
    )
    return parser.parse_known_args()


def filter_forwards(args, exclude):
    """Return only the parts of `args` that do not appear in `exclude`."""
    result = []
    # Start with false, because an unknown argument not starting with a dash
    # probably would just trip pip.
    admitted = False
    for arg in args:
        if not arg.startswith('-'):
            # assume this belongs with the previous argument.
            if admitted:
                result.append(arg)
        elif arg.lstrip('-') in exclude:
            admitted = False
        else:
            result.append(arg)
            admitted = True
    return result


class StdOutFilter(logging.Filter):
    """simple stdout filter for logging"""

    def filter(self, record):
        return record.levelno in [logging.DEBUG, logging.INFO]


def setup_logging(verbose):
    level = logging.DEBUG if verbose else logging.INFO

    format_ = '%(message)s'

    logger = logging.getLogger('pip-review')

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.addFilter(StdOutFilter())
    stdout_handler.setFormatter(logging.Formatter(format_))
    stdout_handler.setLevel(logging.DEBUG)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(logging.Formatter(format_))
    stderr_handler.setLevel(logging.WARNING)

    logger.setLevel(level)
    logger.addHandler(stderr_handler)
    logger.addHandler(stdout_handler)
    return logger


class InteractiveAsker(object):
    """helper for interactive prompting"""

    def __init__(self):
        self.cached_answer = None
        self.last_answer = None

    def ask(self, prompt):
        if self.cached_answer is not None:
            return self.cached_answer

        answer = ''
        while answer not in ['y', 'n', 'a', 'q']:
            question_last = '{0} [Y]es, [N]o, [A]ll, [Q]uit ({1}) '.format(
                prompt, self.last_answer
            )
            question_default = '{0} [Y]es, [N]o, [A]ll, [Q]uit '.format(prompt)
            answer = input(
                question_last if self.last_answer else question_default
            )
            answer = answer.strip().lower()
            answer = self.last_answer if answer == '' else answer

        if answer in ['q', 'a']:
            self.cached_answer = answer
        self.last_answer = answer

        return answer


ask_to_install = partial(InteractiveAsker().ask, prompt='Upgrade now?')


def update_packages(
    packages, forwarded, continue_on_fail, freeze_outdated_packages
):
    upgrade_cmd = PIP_CMD + ['install', '-U'] + forwarded

    if freeze_outdated_packages:
        # py2.7 does not support encoding option in open(), could use codecs.open()
        # pylint: disable=unspecified-encoding
        with open('requirements.txt', 'w') as f:
            for pkg in packages:
                f.write('{}=={}\n'.format(pkg['name'], pkg['version']))
        # pylint: enable=unspecified-encoding

    if not continue_on_fail:
        upgrade_cmd += ['{}'.format(pkg['name']) for pkg in packages]
        subprocess.call(upgrade_cmd, stdout=sys.stdout, stderr=sys.stderr)
        return

    for pkg in packages:
        upgrade_cmd += ['{}'.format(pkg['name'])]
        subprocess.call(upgrade_cmd, stdout=sys.stdout, stderr=sys.stderr)
        upgrade_cmd.pop()


def confirm(question):
    answer = ''
    while answer not in ['y', 'n']:
        answer = input(question)
        answer = answer.strip().lower()
    return answer == 'y'


def parse_legacy(pip_output):
    packages = []
    for line in pip_output.splitlines():
        name_match = NAME_PATTERN.match(line)
        version_matches = [
            match.group() for match in VERSION_PATTERN.finditer(line)
        ]
        if name_match and len(version_matches) == 2:
            packages.append(
                {
                    'name': name_match.group(),
                    'version': version_matches[0],
                    'latest_version': version_matches[1],
                }
            )
    return packages


def get_outdated_packages(forwarded):
    command = PIP_CMD + ['list', '--outdated'] + forwarded
    pip_version = version.parse(pip.__version__)
    if pip_version >= version.parse('6.0'):
        command.append('--disable-pip-version-check')
    if pip_version > version.parse('9.0'):
        command.append('--format=json')
        output = check_output(command).decode('utf-8')
        packages = json.loads(output)
    else:
        output = check_output(command).decode('utf-8').strip()
        packages = parse_legacy(output)
    return packages


# Next two functions describe how to collect data for the
# table. Note how they are not concerned with columns widths.


def extract_column(data, field, title):
    return [title] + list(map(itemgetter(field), data))


def extract_table(outdated):
    return [
        extract_column(outdated, field, title)
        for title, field in COLUMNS.items()
    ]


# Next two functions describe how to format any table. Note that
# they make no assumptions about where the data come from.


def column_width(column):
    return max(map(len, filter(None, column)))


def format_table(columns):
    widths = list(map(column_width, columns))
    row_fmt = ' '.join(map('{{:<{}}}'.format, widths)).format
    ruler = '-' * (sum(widths) + len(widths) - 1)
    rows = list(map(row_fmt, *columns))
    head = rows[0]
    body = rows[1:]
    return '\n'.join([head, ruler] + body + [ruler])


def main():
    args, forwarded = parse_args()
    list_args = filter_forwards(forwarded, INSTALL_ONLY)
    install_args = filter_forwards(forwarded, LIST_ONLY)
    logger = setup_logging(args.verbose)

    if args.raw and args.interactive:
        raise SystemExit('--raw and --interactive cannot be used together')

    outdated = get_outdated_packages(list_args)
    if not outdated and not args.raw:
        logger.info('Everything up-to-date')
        return
    if args.preview or args.preview_only:
        logger.info(format_table(extract_table(outdated)))
        if args.preview_only:
            return
    if args.auto:
        update_packages(
            outdated,
            install_args,
            args.continue_on_fail,
            args.freeze_outdated_packages,
        )
        return
    if args.raw:
        log_messages = (
            '{}=={}'.format(
                pkg.get('name', 'unknown'), pkg.get('latest_version', 'unknown')
            )
            for pkg in outdated
        )
        logger.info('\n'.join(log_messages))
        return

    selected = []
    for pkg in outdated:
        # since we also prompt interactively inside this loop, we cannot do
        # so-called lazy logging - or .. we could but have to go through the
        # same for "pkg in outdated" loop twice, once for logging, once for
        # interactive thus negating any lazy-logging advantage, so pylint: disable
        # pylint: disable=logging-format-interpolation
        logger.info(
            '{}=={} is available (you have {})'.format(
                pkg['name'], pkg['latest_version'], pkg['version']
            )
        )
        # pylint: enable=logging-format-interpolation
        if args.interactive:
            answer = ask_to_install()
            if answer in ['y', 'a']:
                selected.append(pkg)
    if selected:
        update_packages(
            selected,
            install_args,
            args.continue_on_fail,
            args.freeze_outdated_packages,
        )


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.stdout.write('\nAborted\n')
        sys.exit(0)
