# This file is part of Xpra.
# Copyright (C) 2010 Nathaniel Smith <njs@pobox.com>
# Copyright (C) 2011-2013 Antoine Martin <antoine@devloop.org.uk>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

#@PydevCodeAnalysisIgnore

import os as _os
import sys as _sys
import inspect

_init_done = False
def init():
    global _init_done
    if not _init_done:
        _init_done = True
        do_init()

def do_init():
    pass


if _os.name == "nt":
    from win32 import *
elif _sys.platform.startswith("darwin"):
    from darwin import *
elif _os.name == "posix":
    from xposix import *
else:
    raise OSError("Unknown OS %s" % (_os.name))
