# This file is part of Xpra.
# Copyright (C) 2010 Nathaniel Smith <njs@pobox.com>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os.path
import sys
import site


def do_get_install_prefix():
    #special case for "user" installations, ie:
    #$HOME/.local/lib/python2.7/site-packages/xpra/platform/paths.py
    try:
        base = site.getuserbase()
    except:
        base = site.USER_BASE
    if __file__.startswith(base):
        return base
    return sys.prefix

def do_get_resources_dir():
    #is there a better/cleaner way?
    from xpra.platform.paths import get_install_prefix
    options = [get_install_prefix(),
               sys.exec_prefix,
               "/usr",
               "/usr/local"]
    for x in options:
        p = os.path.join(x, "share", "xpra")
        if os.path.exists(p) and os.path.isdir(p):
            return p
    try:
        # test for a local installation path (run from source tree):
        local_share_path = os.path.abspath(os.path.join(os.path.dirname(sys.argv[0]), "..", "share", "xpra"))
        if os.path.exists(local_share_path) and os.path.isdir(local_share_path):
            return local_share_path
    except:
        pass
    return os.getcwd()

def do_get_app_dir():
    from xpra.platform.paths import get_resources_dir
    return get_resources_dir()

def do_get_icon_dir():
    from xpra.platform.paths import get_app_dir
    return os.path.join(get_app_dir(), "icons")

def do_get_socket_dirs():
    SOCKET_DIRS = ["~/.xpra"]   #the old default
    #added in 0.16, support for /run:
    if os.path.exists("/var/run/user") and os.path.isdir("/var/run/user"):
        #private, per user: /run/user/1000/xpra
        SOCKET_DIRS.append("/var/run/user/$UID/xpra")
        #for shared sockets:
        SOCKET_DIRS.append("/var/run/xpra")
    return SOCKET_DIRS
