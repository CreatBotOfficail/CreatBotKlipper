# Helpers for reporting long-running operation progress to host clients.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import re


ACTION_PREFIX = 'action:operation_'
_TOKEN_RE = re.compile(r'^[a-z][a-z0-9_]*$')


def respond(gcode, action, **fields):
    """Emit the same host response as RESPOND TYPE=command."""
    if not _TOKEN_RE.match(action):
        raise ValueError("Invalid operation action: %s" % (action,))
    parts = []
    for name, value in fields.items():
        if not _TOKEN_RE.match(name):
            raise ValueError("Invalid operation field: %s" % (name,))
        value = str(value)
        if not value or any(char.isspace() for char in value):
            raise ValueError("Invalid operation value for %s" % (name,))
        parts.append('%s=%s' % (name, value))
    message = '// %s%s' % (ACTION_PREFIX, action)
    if parts:
        message += ' ' + ' '.join(parts)
    gcode.respond_raw(message)
