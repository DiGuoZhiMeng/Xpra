#!/usr/bin/env python

# This file is part of Xpra.
# Copyright (C) 2010-2014 Antoine Martin <antoine@devloop.org.uk>
# Copyright (C) 2008, 2009, 2010 Nathaniel Smith <njs@pobox.com>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

##############################################################################
# FIXME: Cython.Distutils.build_ext leaves crud in the source directory.  (So
# does the make_constants hack.)

import glob
from distutils.core import setup
from distutils.extension import Extension
import subprocess, sys, traceback
import os.path
import stat
try:
    from hashlib import md5         #@UnusedImport
    new_md5 = md5
except:
    import md5                      #@Reimport
    new_md5 = md5.new

print(" ".join(sys.argv))

#*******************************************************************************
# build options, these may get modified further down..
#
import xpra
setup_options = {}
setup_options["name"] = "xpra"
setup_options["author"] = "Antoine Martin"
setup_options["author_email"] = "antoine@devloop.org.uk"
setup_options["version"] = xpra.__version__
setup_options["url"] = "http://xpra.org/"
setup_options["download_url"] = "http://xpra.org/src/"
setup_options["description"] = "Xpra: 'screen for X' utility"

xpra_desc = "'screen for X' -- a tool to detach/reattach running X programs"
setup_options["long_description"] = xpra_desc
data_files = []
setup_options["data_files"] = data_files
modules = []
setup_options["py_modules"] = modules
packages = []       #used by py2app and py2exe
excludes = []       #only used by py2exe on win32
ext_modules = []
cmdclass = {}
scripts = []


WIN32 = sys.platform.startswith("win")
OSX = sys.platform.startswith("darwin")
PYTHON3 = sys.version_info[0] == 3
if PYTHON3 and not WIN32:
    #(on win32, we do this through the BAT file because it conflicts with cx_freeze)
    from lib2to3 import refactor
    from distutils.command.build_py import build_py_2to3    #@UnresolvedImport
    #fixers = refactor.get_fixers_from_package("lib2to3.fixes")
    fixers = [fix for fix in refactor.get_fixers_from_package("lib2to3.fixes")
               if fix.split('fix_')[-1] not in ('next',)
               ]
    build_py_2to3.fixer_names = fixers
    cmdclass['build_py'] = build_py_2to3


#*******************************************************************************
# Most of the options below can be modified on the command line
# using --with-OPTION or --without-OPTION
# only the default values are specified here:
#*******************************************************************************
def get_status_output(*args, **kwargs):
    try:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
        p = subprocess.Popen(*args, **kwargs)
    except:
        e = sys.exc_info()[1]
        print("error running %s,%s: %s" % (args, kwargs, e))
        return -1, "", ""
    stdout, stderr = p.communicate()
    return p.returncode, stdout, stderr
PKG_CONFIG = os.environ.get("PKG_CONFIG", "pkg-config")
has_pkg_config = False
#we don't support building with "pkg-config" on win32 with python2:
if PKG_CONFIG and (PYTHON3 or not WIN32):
    has_pkg_config = get_status_output([PKG_CONFIG, "--version"])[0]==0

def pkg_config_ok(*args, **kwargs):
    if not has_pkg_config:
        return kwargs.get("fallback", False)
    cmd = [PKG_CONFIG]  + [str(x) for x in args]
    return get_status_output(cmd)[0]==0

from xpra.platform.features import LOCAL_SERVERS_SUPPORTED, SHADOW_SUPPORTED
shadow_ENABLED = SHADOW_SUPPORTED
server_ENABLED = (LOCAL_SERVERS_SUPPORTED or shadow_ENABLED) and not PYTHON3
client_ENABLED = True

x11_ENABLED = not WIN32 and not OSX
gtk_x11_ENABLED = not WIN32 and not OSX
argb_ENABLED = True
gtk2_ENABLED = client_ENABLED and not PYTHON3
gtk3_ENABLED = PYTHON3
qt4_ENABLED = False
opengl_ENABLED = client_ENABLED
html5_ENABLED = not WIN32 and not OSX

bencode_ENABLED         = True
cython_bencode_ENABLED  = True
rencode_ENABLED         = True
cymaths_ENABLED         = True
cyxor_ENABLED           = True
clipboard_ENABLED       = not PYTHON3
Xdummy_ENABLED          = None          #None means auto-detect
if WIN32 or OSX:
    Xdummy_ENABLED = False
sound_ENABLED           = True

enc_proxy_ENABLED       = True
enc_x264_ENABLED        = True          #too important to detect
enc_x265_ENABLED        = pkg_config_ok("--exists", "x265")
webp_ENABLED            = pkg_config_ok("--atleast-version=0.3", "libwebp", fallback=WIN32)
x264_static_ENABLED     = False
x265_static_ENABLED     = False
vpx_ENABLED             = pkg_config_ok("--atleast-version=1.0", "vpx", fallback=WIN32) or pkg_config_ok("--atleast-version=1.0", "libvpx", fallback=WIN32)
vpx_static_ENABLED      = False
#ffmpeg 1.x and libav:
dec_avcodec_ENABLED     = pkg_config_ok("--max-version=55", "libavcodec", fallback=not WIN32)
#ffmpeg 2 onwards:
dec_avcodec2_ENABLED    = pkg_config_ok("--atleast-version=55", "libavcodec", fallback=WIN32)
# some version strings I found:
# Fedora 19: 54.92.100
# Fedora 20: 55.39.101
# Debian sid and jessie: 55.34.1
# Debian wheezy: 53.35
avcodec_static_ENABLED  = False
avcodec2_static_ENABLED = False
csc_swscale_ENABLED     = pkg_config_ok("--exists", "libswscale", fallback=WIN32)
swscale_static_ENABLED  = False
csc_cython_ENABLED      = True
webm_ENABLED            = sys.version_info>=(2,6)       #needs absolute imports
nvenc_ENABLED           = pkg_config_ok("--exists", "nvenc3")       #or os.path.exists("C:\\nvenc_3.0_windows_sdk")
csc_opencl_ENABLED      = pkg_config_ok("--exists", "OpenCL")
memoryview_ENABLED      = PYTHON3

warn_ENABLED            = True
strict_ENABLED          = True
PIC_ENABLED             = not WIN32     #ming32 moans that it is always enabled already
debug_ENABLED           = False
verbose_ENABLED         = False
bundle_tests_ENABLED    = False

#allow some of these flags to be modified on the command line:
SWITCHES = ("enc_x264", "x264_static",
            "enc_x265", "x265_static",
            "nvenc",
            "dec_avcodec", "avcodec_static",
            "dec_avcodec2", "avcodec2_static",
            "csc_swscale", "swscale_static",
            "csc_opencl", "csc_cython",
            "vpx", "vpx_static",
            "webp", "webm",
            "memoryview",
            "rencode", "bencode", "cython_bencode",
            "clipboard",
            "server", "client", "x11", "gtk_x11",
            "gtk2", "gtk3", "qt4", "html5",
            "sound", "cyxor", "cymaths", "opengl", "argb",
            "warn", "strict", "shadow", "debug", "PIC", "Xdummy", "verbose", "bundle_tests")
HELP = "-h" in sys.argv or "--help" in sys.argv
if HELP:
    setup()
    print("Xpra specific build and install switches:")
    for x in SWITCHES:
        d = vars()["%s_ENABLED" % x]
        with_str = "  --with-%s" % x
        without_str = "  --without-%s" % x
        if d is True or d is False:
            default_str = str(d)
        else:
            default_str = "auto-detect"
        print("%s or %s (default: %s)" % (with_str.ljust(25), without_str.ljust(30), default_str))
    sys.exit(0)

filtered_args = []
for arg in sys.argv:
    #deprecated flag:
    if arg == "--enable-Xdummy":
        Xdummy_ENABLED = True
        continue
    matched = False
    for x in SWITCHES:
        if arg=="--with-%s" % x:
            vars()["%s_ENABLED" % x] = True
            matched = True
            break
        elif arg=="--without-%s" % x:
            vars()["%s_ENABLED" % x] = False
            matched = True
            break
    if not matched:
        filtered_args.append(arg)
sys.argv = filtered_args
if "clean" not in sys.argv:
    switches_info = {}
    for x in SWITCHES:
        switches_info[x] = vars()["%s_ENABLED" % x]
    print("build switches:")
    for k in sorted(switches_info.keys()):
        v = switches_info[k]
        print("* %s : %s" % (str(k).ljust(20), {None : "Auto", True : "Y", False : "N"}.get(v, v)))

    #sanity check the flags:
    if clipboard_ENABLED and not server_ENABLED and not gtk2_ENABLED and not gtk3_ENABLED:
        print("Warning: clipboard can only be used with the server or one of the gtk clients!")
        clipboard_ENABLED = False
    if opengl_ENABLED and not gtk2_ENABLED:
        print("Warning: opengl can only be used with the gtk2 clients")
        opengl_ENABLED = False
    if shadow_ENABLED and not server_ENABLED:
        print("Warning: shadow requires server to be enabled!")
        shadow_ENABLED = False
    if cymaths_ENABLED and not server_ENABLED:
        print("Warning: cymaths requires server to be enabled!")
        cymaths_ENABLED = False
    if x11_ENABLED and WIN32:
        print("Warning: enabling x11 on MS Windows is unlikely to work!")
    if gtk_x11_ENABLED and not x11_ENABLED:
        print("Error: you must enable x11 to support gtk_x11!")
        exit(1)
    if client_ENABLED and not gtk2_ENABLED and not gtk3_ENABLED and not qt4_ENABLED:
        print("Warning: client is enabled but none of the client toolkits are!?")
    if not argb_ENABLED and (x11_ENABLED or OSX):
        print("Error: argb is required for x11 and osx builds!")
        exit(1)
    if not client_ENABLED and not server_ENABLED:
        print("Error: you must build at least the client or server!")
        exit(1)
    if memoryview_ENABLED and sys.version<"2.7":
        print("Error: memoryview support requires Python version 2.7 or greater")
        exit(1)


#*******************************************************************************
# default sets:

external_includes = ["Crypto", "Crypto.Cipher",
                     "hashlib",
                     "PIL", "PIL.Image",
                     "ctypes", "platform"]
if gtk3_ENABLED:
    external_includes += ["gi"]
elif gtk2_ENABLED or x11_ENABLED:
    external_includes += "cairo", "pango", "pangocairo", "atk", "glib", "gobject", "gio", "gtk.keysyms"

external_excludes = [
                    #Tcl/Tk
                    "Tkconstants", "Tkinter", "tcl",
                    #PIL bits that import TK:
                    "_imagingtk", "PIL._imagingtk", "ImageTk", "PIL.ImageTk", "FixTk",
                    #formats we don't use:
                    "GimpGradientFile", "GimpPaletteFile", "BmpImagePlugin", "TiffImagePlugin",
                    #not used:
                    "curses", "email", "mimetypes", "mimetools", "pdb",
                    "urllib", "urllib2", "tty",
                    "ssl", "_ssl",
                    "cookielib", "BaseHTTPServer", "ftplib", "httplib", "fileinput",
                    "distutils", "setuptools", "doctest"
                    ]



#because of differences in how we specify packages and modules
#for distutils / py2app and py2exe
#use the following functions, which should get the right
#data in the global variables "packages", "modules" and "excludes"

def remove_packages(*mods):
    """ ensures that the given packages are not included:
        removes them from the "modules" and "packages" list and adds them to "excludes" list
    """
    global packages, modules, excludes
    for m in list(modules):
        for x in mods:
            if m.startswith(x):
                modules.remove(m)
                break
    for x in mods:
        if x in packages:
            packages.remove(x)
        if x not in excludes:
            excludes.append(x)

def add_packages(*pkgs):
    """ adds the given packages to the packages list,
        and adds all the modules found in this package (including the package itself)
    """
    global packages
    for x in pkgs:
        if x not in packages:
            packages.append(x)
    add_modules(*pkgs)

def add_modules(*mods):
    """ adds the packages and any .py module found in the packages to the "modules" list
    """
    global modules
    for x in mods:
        pathname = os.path.sep.join(x.split("."))
        #is this a file module?
        f = "%s.py" % pathname
        if os.path.exists(f) and os.path.isfile(f):
            if x not in modules:
                modules.append(x)
        if os.path.exists(pathname) and os.path.isdir(pathname):
            #add all file modules found in this directory
            for f in os.listdir(pathname):
                #make sure we only include python files,
                #and ignore eclipse copies
                if f.endswith(".py") and not f.startswith("Copy ")<0:
                    fname = os.path.join(pathname, f)
                    if os.path.isfile(fname):
                        modname = "%s.%s" % (x, f.replace(".py", ""))
                        modules.append(modname)

def toggle_packages(enabled, *module_names):
    if enabled:
        add_packages(*module_names)
    else:
        remove_packages(*module_names)

#always included:
add_modules("xpra",
            "xpra.platform",
            "xpra.codecs",
            "xpra.codecs.xor")
add_packages("xpra.scripts", "xpra.keyboard", "xpra.net")


def add_data_files(target_dir, files):
    #this is overriden below because cx_freeze uses the opposite structure (files first...). sigh.
    assert type(target_dir)==str
    assert type(files) in (list, tuple)
    data_files.append((target_dir, files))


def check_md5sums(md5sums):
    print("Verifying md5sums:")
    for filename, md5sum in md5sums.items():
        if not os.path.exists(filename) or not os.path.isfile(filename):
            sys.exit("ERROR: file %s is missing or not a file!" % filename)
        sys.stdout.write("* %s: " % str(filename).ljust(52))
        f = open(filename, mode='rb')
        data = f.read()
        f.close()
        m = new_md5()
        m.update(data)
        digest = m.hexdigest()
        assert digest==md5sum, "md5 digest for file %s does not match, expected %s but found %s" % (filename, md5sum, digest)
        sys.stdout.write("OK\n")
        sys.stdout.flush()

#for pretty printing of options:
def print_option(prefix, k, v):
    if type(v)==dict:
        print("%s* %s:" % (prefix, k))
        for kk,vv in v.items():
            print_option(" "+prefix, kk, vv)
    else:
        print("%s* %s=%s" % (prefix, k, v))
def print_dict(d):
    for k,v in d.items():
        print_option("", k, v)

#*******************************************************************************
# Utility methods for building with Cython
def cython_version_check(min_version):
    try:
        from Cython.Compiler.Version import version as cython_version
    except ImportError:
        e = sys.exc_info()[1]
        sys.exit("ERROR: Cannot find Cython: %s" % e)
    from distutils.version import LooseVersion
    if LooseVersion(cython_version) < LooseVersion(".".join([str(x) for x in min_version])):
        sys.exit("ERROR: Your version of Cython is too old to build this package\n"
                 "You have version %s\n"
                 "Please upgrade to Cython %s or better"
                 % (cython_version, ".".join([str(part) for part in min_version])))

def cython_add(extension, min_version=(0, 19, 0)):
    #gentoo does weird things, calls --no-compile with build *and* install
    #then expects to find the cython modules!? ie:
    #python2.7 setup.py build -b build-2.7 install --no-compile --root=/var/tmp/portage/x11-wm/xpra-0.7.0/temp/images/2.7
    if "--no-compile" in sys.argv and not ("build" in sys.argv and "install" in sys.argv):
        return
    cython_version_check(min_version)
    from Cython.Distutils import build_ext
    ext_modules.append(extension)
    cmdclass = {'build_ext': build_ext}

def add_to_keywords(kw, key, *args):
    values = kw.setdefault(key, [])
    for arg in args:
        values.append(arg)
def remove_from_keywords(kw, key, value):
    values = kw.get(key)
    if values and value in values:
        values.remove(value)


def checkdirs(*dirs):
    for d in dirs:
        if not os.path.exists(d) or not os.path.isdir(d):
            raise Exception("cannot find a directory which is required for building: %s" % d)

PYGTK_PACKAGES = ["pygobject-2.0", "pygtk-2.0"]

GCC_VERSION = []
def get_gcc_version():
    global GCC_VERSION
    if len(GCC_VERSION)==0:
        cmd = [os.environ.get("CC", "gcc"), "-v"]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        output, _ = proc.communicate()
        status = proc.wait()
        if status==0:
            V_LINE = "gcc version "
            for line in output.decode("utf8").splitlines():
                if line.startswith(V_LINE):
                    v_str = line[len(V_LINE):].split(" ")[0]
                    for p in v_str.split("."):
                        try:
                            GCC_VERSION.append(int(p))
                        except:
                            break
                    print("found gcc version: %s" % ".".join([str(x) for x in GCC_VERSION]))
                    break
    return GCC_VERSION

def make_constants_pxi(constants_path, pxi_path, **kwargs):
    constants = []
    for line in open(constants_path):
        data = line.split("#", 1)[0].strip()
        # data can be empty ''...
        if not data:
            continue
        # or a pair like 'cFoo "Foo"'...
        elif len(data.split()) == 2:
            (pyname, cname) = data.split()
            constants.append((pyname, cname))
        # or just a simple token 'Foo'
        else:
            constants.append(data)
    out = open(pxi_path, "w")
    out.write("cdef extern from *:\n")
    ### Apparently you can't use | on enum's?!
    # out.write("    enum MagicNumbers:\n")
    # for const in constants:
    #     if isinstance(const, tuple):
    #         out.write('        %s %s\n' % const)
    #     else:
    #         out.write('        %s\n' % (const,))
    for const in constants:
        if isinstance(const, tuple):
            out.write('    unsigned int %s %s\n' % const)
        else:
            out.write('    unsigned int %s\n' % (const,))

    out.write("constants = {\n")
    for const in constants:
        if isinstance(const, tuple):
            pyname = const[0]
        else:
            pyname = const
        out.write('    "%s": %s,\n' % (pyname, pyname))
    out.write("}\n")

    if kwargs:
        out.write("\n\n")
        for k, v in kwargs.items():
            out.write('DEF %s = %s\n' % (k, v))


def make_constants(*paths, **kwargs):
    base = os.path.join(os.getcwd(), *paths)
    constants_file = "%s.txt" % base
    pxi_file = "%s.pxi" % base
    reason = None
    if not os.path.exists(pxi_file):
        reason = "no pxi file"
    elif os.path.getctime(pxi_file)<os.path.getctime(constants_file):
        reason = "pxi file out of date"
    elif os.path.getctime(pxi_file)<os.path.getctime(__file__):
        reason = "newer build file"
    if reason:
        if verbose_ENABLED:
            print("(re)generating %s (%s):" % (pxi_file, reason))
        make_constants_pxi(constants_file, pxi_file, **kwargs)


def static_link_args(*libnames):
    return ["-Wl,-Bstatic"] + ["-l%s" % x for x in libnames] + ["-Wl,-Bsymbolic", "-Wl,-Bdynamic"]

def get_static_pkgconfig(*libnames):
    defs = pkgconfig()
    remove_from_keywords(defs, 'extra_compile_args', '-fsanitize=address')
    if os.name=="posix":
        if debug_ENABLED:
            add_to_keywords(defs, 'extra_link_args', "-Wl,--verbose")
        defs.update({'include_dirs': ["/usr/include/xpra", "/usr/local/include"],
                     'library_dirs': ["/usr/lib64/xpra", "/usr/lib/xpra", "/usr/local/lib", "/usr/local/lib64"]})
    if len(libnames)>0:
        add_to_keywords(defs,  'extra_link_args', *static_link_args(*libnames))
    return defs

# Tweaked from http://aspn.activestate.com/ASPN/Cookbook/Python/Recipe/502261
def pkgconfig(*pkgs_options, **ekw):
    kw = dict(ekw)
    static = kw.get("static", None)
    if static is not None:
        del kw["static"]
        if static:
            return get_static_pkgconfig(*pkgs_options)
    if kw.get("optimize"):
        optimize = kw["optimize"]
        del kw["optimize"]
        if type(optimize)==bool:
            optimize = int(optimize)*3
        add_to_keywords(kw, 'extra_compile_args', "-O%i" % optimize)
    ignored_flags = []
    if kw.get("ignored_flags"):
        ignored_flags = kw.get("ignored_flags")
        del kw["ignored_flags"]

    if len(pkgs_options)>0:
        package_names = []
        #find out which package name to use from potentially many options
        #and bail out early with a meaningful error if we can't find any valid options
        for package_options in pkgs_options:
            #for this package options, find the ones that work
            valid_option = None
            if type(package_options)==str:
                options = [package_options]     #got given just one string
                if not package_options.startswith("lib"):
                    options.append("lib%s" % package_options)
            else:
                assert type(package_options)==list
                options = package_options       #got given a list of options
            for option in options:
                cmd = ["pkg-config", "--exists", option]
                proc = subprocess.Popen(cmd, stdin=None, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                status = proc.wait()
                if status==0:
                    valid_option = option
                    break
            if not valid_option:
                sys.exit("ERROR: cannot find a valid pkg-config package for %s" % (options,))
            package_names.append(valid_option)
        if verbose_ENABLED and list(pkgs_options)!=list(package_names):
            print("pkgconfig(%s,%s) using package names=%s" % (pkgs_options, ekw, package_names))
        flag_map = {'-I': 'include_dirs',
                    '-L': 'library_dirs',
                    '-l': 'libraries'}
        cmd = ["pkg-config", "--libs", "--cflags", "%s" % (" ".join(package_names),)]
        proc = subprocess.Popen(cmd, env=os.environ, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (output, _) = proc.communicate()
        status = proc.wait()
        if status!=0:
            sys.exit("ERROR: call to pkg-config ('%s') failed" % " ".join(cmd))
        if sys.version>='3':
            output = output.decode('utf-8')
        for token in output.split():
            if token[:2] in ignored_flags:
                pass
            elif token[:2] in flag_map:
                add_to_keywords(kw, flag_map.get(token[:2]), token[2:])
            else: # throw others to extra_link_args
                add_to_keywords(kw, 'extra_link_args', token)
            for k, v in kw.items(): # remove duplicates
                kw[k] = list(set(v))
    if warn_ENABLED:
        add_to_keywords(kw, 'extra_compile_args', "-Wall")
        add_to_keywords(kw, 'extra_link_args', "-Wall")
    if strict_ENABLED:
        #these are almost certainly real errors since our code is "clean":
        if get_gcc_version()>=[4, 4]:
            eifd = "-Werror=implicit-function-declaration"
        else:
            eifd = "-Werror-implicit-function-declaration"
        add_to_keywords(kw, 'extra_compile_args', eifd)
    if PIC_ENABLED:
        add_to_keywords(kw, 'extra_compile_args', "-fPIC")
    if debug_ENABLED:
        add_to_keywords(kw, 'extra_compile_args', '-g')
        add_to_keywords(kw, 'extra_compile_args', '-ggdb')
        if get_gcc_version()>=[4, 8]:
            add_to_keywords(kw, 'extra_compile_args', '-fsanitize=address')
            add_to_keywords(kw, 'extra_link_args', '-fsanitize=address')
    #add_to_keywords(kw, 'include_dirs', '.')
    if verbose_ENABLED:
        print("pkgconfig(%s,%s)=%s" % (pkgs_options, ekw, kw))
    return kw


#*******************************************************************************
def get_xorg_conf_and_script():
    if not server_ENABLED:
        return "etc/xpra/client-only/xpra.conf", False

    def Xvfb():
        return "etc/xpra/Xvfb/xpra.conf", False

    if sys.platform.find("bsd")>=0:
        print("Warning: sorry, no support for Xdummy on %s" % sys.platform)
        return Xvfb()

    XORG_BIN = None
    PATHS = os.environ.get("PATH").split(os.pathsep)
    for x in PATHS:
        xorg = os.path.join(x, "Xorg")
        if os.path.isfile(xorg):
            XORG_BIN = xorg
            break
    if not XORG_BIN:
        print("Xorg not found, cannot detect version or Xdummy support")
        return Xvfb()

    def Xorg_suid_check():
        xorg_stat = os.stat(XORG_BIN)
        if (xorg_stat.st_mode & stat.S_ISUID)!=0:
            if (xorg_stat.st_mode & stat.S_IROTH)==0:
                print("Xorg is suid and not readable, Xdummy support unavailable")
                return Xvfb()
            print("%s is suid and readable, using the xpra_Xdummy wrapper" % XORG_BIN)
            return "etc/xpra/xpra_Xdummy/xpra.conf", True
        else:
            print("using Xdummy config file")
            return "etc/xpra/Xdummy/xpra.conf", False

    if Xdummy_ENABLED is False:
        return Xvfb()
    elif Xdummy_ENABLED is True:
        print("Xdummy support specified as 'enabled', will detect suid mode")
        return Xorg_suid_check()
    else:
        print("Xdummy support unspecified, will try to detect")

    cmd = ["lsb_release", "-cs"]
    try:
        proc = subprocess.Popen(cmd, stdin=None, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = proc.communicate()
        release = out.replace("\n", "")
        print("Found OS release: %s" % release)
        if release in ("raring", "saucy", "trusty"):
            #yet another instance of Ubuntu breaking something
            print("Warning: Ubuntu '%s' breaks Xorg/Xdummy usage - using Xvfb fallback" % release)
            return  Xvfb()
    except:
        e = sys.exc_info()[1]
        print("failed to detect OS release using %s: %s" % (" ".join(cmd), e))

    #do live detection
    #fedora rawhide binary:
    if os.path.exists("/usr/libexec/Xorg.bin"):
        cmd = ["/usr/libexec/Xorg.bin", "-version"]
    else:
        cmd = ["Xorg", "-version"] 
    if verbose_ENABLED:
        print("detecting Xorg version using: %s" % str(cmd))
    try:
        proc = subprocess.Popen(cmd, stdin=None, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = proc.communicate()
        V_LINE = "X.Org X Server "
        xorg_version = None
        for line in out.decode("utf8").splitlines():
            if line.startswith(V_LINE):
                v_str = line[len(V_LINE):]
                xorg_version = [int(x) for x in v_str.split(".")[:2]]
                break
        if not xorg_version:
            print("Xorg version could not be detected, Xdummy support unavailable")
            return Xvfb()
        if xorg_version<[1, 12]:
            print("Xorg version %s is too old (1.12 or later required), Xdummy support not available" % v_str)
            return Xvfb()
        print("found valid recent version of Xorg server: %s" % v_str)
        return Xorg_suid_check()
    except:
        e = sys.exc_info()[1]
        print("failed to detect Xorg version: %s" % e)
        print("not installing Xdummy support")
        traceback.print_exc()
        return  Xvfb()


#*******************************************************************************
if 'clean' in sys.argv or 'sdist' in sys.argv:
    #clean and sdist don't actually use cython,
    #so skip this (and avoid errors)
    def pkgconfig(*pkgs_options, **ekw):
        return {}
    #always include everything in this case:
    add_packages("xpra")
    #ensure we remove the files we generate:
    CLEAN_FILES = [
                   "xpra/gtk_common/gdk_atoms.c",
                   "xpra/x11/gtk_x11/constants.pxi",
                   "xpra/x11/gtk_x11/gdk_bindings.c",
                   "xpra/x11/gtk_x11/gdk_display_source.c",
                   "xpra/x11/gtk3_x11/gdk_display_source.c",
                   "xpra/x11/bindings/constants.pxi",
                   "xpra/x11/bindings/wait_for_x_server.c",
                   "xpra/x11/bindings/keyboard_bindings.c",
                   "xpra/x11/bindings/display_source.c",
                   "xpra/x11/bindings/window_bindings.c",
                   "xpra/x11/bindings/randr_bindings.c",
                   "xpra/x11/bindings/core_bindings.c",
                   "xpra/x11/bindings/ximage.c",
                   "xpra/net/rencode/rencode.c",
                   "xpra/net/bencode/cython_bencode.c",
                   "xpra/codecs/vpx/encoder.c",
                   "xpra/codecs/vpx/decoder.c",
                   "xpra/codecs/nvenc/encoder.c",
                   "xpra/codecs/nvenc/constants.pxi",
                   "xpra/codecs/enc_x264/encoder.c",
                   "xpra/codecs/enc_x265/encoder.c",
                   "xpra/codecs/webp/encode.c",
                   "xpra/codecs/webp/decode.c",
                   "xpra/codecs/dec_avcodec/decoder.c",
                   "xpra/codecs/dec_avcodec/constants.pxi",
                   "xpra/codecs/dec_avcodec2/decoder.c",
                   "xpra/codecs/csc_swscale/colorspace_converter.c",
                   "xpra/codecs/csc_swscale/constants.pxi",
                   "xpra/codecs/csc_cython/colorspace_converter.c",
                   "xpra/codecs/xor/cyxor.c",
                   "xpra/codecs/argb/argb.c",
                   "xpra/server/stats/cymaths.c",
                   "etc/xpra/xpra.conf"]
    if sys.platform.startswith("win"):
        #on win32, the build creates ".pyd" files, clean those too:
        for x in list(CLEAN_FILES):
            if x.endswith(".c"):
                CLEAN_FILES.append(x[:-2]+".pyd")
    if 'clean' in sys.argv:
        CLEAN_FILES.append("xpra/build_info.py")
    for x in CLEAN_FILES:
        filename = os.path.join(os.getcwd(), x.replace("/", os.path.sep))
        if os.path.exists(filename):
            if verbose_ENABLED:
                print("removing Cython/build generated file: %s" % x)
            os.unlink(filename)

from add_build_info import record_build_info, record_src_info, has_src_info

if "clean" not in sys.argv:
    # Add build info to build_info.py file:
    record_build_info()

if "sdist" in sys.argv:
    record_src_info()

if "install" in sys.argv:
    #if installing from source tree rather than
    #from a source snapshot, we may not have a "src_info" file
    #so create one:
    if not has_src_info():
        record_src_info()


if 'clean' in sys.argv or 'sdist' in sys.argv:
    #take shortcut to skip cython/pkgconfig steps:
    setup(**setup_options)
    sys.exit(0)



def glob_recurse(srcdir):
    m = {}
    for root, _, files in os.walk(srcdir):
        for f in files:
            dirname = root[len(srcdir)+1:]
            filename = os.path.join(root, f)
            m.setdefault(dirname, []).append(filename)
    return m

#*******************************************************************************
if WIN32:
    add_packages("xpra.platform.win32")
    remove_packages("xpra.platform.darwin", "xpra.platform.xposix")

    ###########################################################
    #START OF HARDCODED SECTION
    #this should all be done with pkgconfig...
    #but until someone figures this out, the ugly path code below works
    #as long as you install in the same place or tweak the paths.

    #ffmpeg is needed for both swscale and x264:
    libffmpeg_path = ""
    if dec_avcodec_ENABLED:
        assert not dec_avcodec2_ENABLED, "cannot enable both dec_avcodec and dec_avcodec2"
        libffmpeg_path = "C:\\ffmpeg-win32-bin"
    elif dec_avcodec2_ENABLED:
        assert not dec_avcodec_ENABLED, "cannot enable both dec_avcodec and dec_avcodec2"
        libffmpeg_path = "C:\\ffmpeg2-win32-bin"
    else:
        if csc_swscale_ENABLED:
            for p in ("C:\\ffmpeg2-win32-bin", "C:\\ffmpeg-win32-bin"):
                if os.path.exists(p):
                    libffmpeg_path = p
            assert libffmpeg_path is not None, "no ffmpeg found, cannot use csc_swscale"
    libffmpeg_include_dir   = os.path.join(libffmpeg_path, "include")
    libffmpeg_lib_dir       = os.path.join(libffmpeg_path, "lib")
    libffmpeg_bin_dir       = os.path.join(libffmpeg_path, "bin")
    #x265
    x265_path ="C:\\x265"
    x265_include_dir    = x265_path
    x265_lib_dir        = x265_path
    x265_bin_dir        = x265_path
    #x264 (direct from build dir.. yuk - sorry!):
    x264_path ="C:\\x264"
    x264_include_dir    = x264_path
    x264_lib_dir        = x264_path
    x264_bin_dir        = x264_path
    # Same for vpx:
    # http://code.google.com/p/webm/downloads/list
    #the path after installing may look like this:
    #vpx_PATH="C:\\vpx-vp8-debug-src-x86-win32mt-vs9-v1.1.0"
    #but we use something more generic, without the version numbers:
    vpx_path = ""
    for p in ("C:\\vpx-1.3", "C:\\vpx-1.2", "C:\\vpx-1.1", "C:\\vpx-vp8"):
        if os.path.exists(p) and os.path.isdir(p):
            vpx_path = p
            break
    vpx_include_dir     = os.path.join(vpx_path, "include")
    vpx_lib_dir         = os.path.join(vpx_path, "lib", "Win32")
    vpx_bin_dir         = os.path.join(vpx_path, "lib", "Win32")
    if os.path.exists(os.path.join(vpx_lib_dir, "vpx.lib")):
        vpx_lib_names = ["vpx"]               #for libvpx 1.3.0
    elif os.path.exists(os.path.join(vpx_lib_dir, "vpxmd.lib")):
        vpx_lib_names = ["vpxmd"]             #for libvpx 1.2.0
    else:
        vpx_lib_names = ["vpxmt", "vpxmtd"]   #for libvpx 1.1.0
    #webp:
    webp_path = "C:\\libwebp-windows-x86"
    webp_include_dir    = webp_path+"\\include"
    webp_lib_dir        = webp_path+"\\lib"
    webp_bin_dir        = webp_path+"\\bin"
    webp_lib_names      = ["libwebp"]
    #cuda:
    cuda_path = "C:\\Program Files\\NVIDIA GPU Computing Toolkit\\CUDA\\v5.5"
    cuda_include_dir    = os.path.join(cuda_path, "include")
    cuda_lib_dir        = os.path.join(cuda_path, "lib", "Win32")
    cuda_bin_dir        = os.path.join(cuda_path, "bin")
    #nvenc:
    nvenc_path = "C:\\nvenc_3.0_windows_sdk"
    nvenc_include_dir       = nvenc_path + "\\Samples\\nvEncodeApp\\inc"
    nvenc_core_include_dir  = nvenc_path + "\\Samples\\core\\include"
    #let's not use crazy paths, just copy the dll somewhere that makes sense:
    nvenc_bin_dir           = nvenc_path + "\\bin\\win32\\release"
    nvenc_lib_names         = []    #not linked against it, we use dlopen!

    # Same for PyGTK:
    # http://www.pygtk.org/downloads.html
    gtk2_path = "C:\\Python27\\Lib\\site-packages\\gtk-2.0"
    python_include_path = "C:\\Python27\\include"
    gtk2runtime_path        = os.path.join(gtk2_path, "runtime")
    gtk2_lib_dir            = os.path.join(gtk2runtime_path, "bin")
    gtk2_base_include_dir   = os.path.join(gtk2runtime_path, "include")

    pygtk_include_dir       = os.path.join(python_include_path, "pygtk-2.0")
    atk_include_dir         = os.path.join(gtk2_base_include_dir, "atk-1.0")
    gtk2_include_dir        = os.path.join(gtk2_base_include_dir, "gtk-2.0")
    gdkpixbuf_include_dir   = os.path.join(gtk2_base_include_dir, "gdk-pixbuf-2.0")
    glib_include_dir        = os.path.join(gtk2_base_include_dir, "glib-2.0")
    cairo_include_dir       = os.path.join(gtk2_base_include_dir, "cairo")
    pango_include_dir       = os.path.join(gtk2_base_include_dir, "pango-1.0")
    gdkconfig_include_dir   = os.path.join(gtk2runtime_path, "lib", "gtk-2.0", "include")
    glibconfig_include_dir  = os.path.join(gtk2runtime_path, "lib", "glib-2.0", "include")
    #END OF HARDCODED SECTION
    ###########################################################


    #only add the py2exe / cx_freeze specific options
    #if we aren't just building the Cython bits with "build_ext":
    if "build_ext" not in sys.argv:
        #with py2exe and cx_freeze, we don't use py_modules
        del setup_options["py_modules"]
        external_includes += ["win32con", "win32gui", "win32process", "win32api"]
        if PYTHON3:
            from cx_Freeze import setup, Executable     #@UnresolvedImport @Reimport
            import site
            #ie: C:\Python3.5\Lib\site-packages\
            site_dir = site.getsitepackages()[1]
            #this is where the installer I have used put things:
            include_dll_path = os.path.join(site_dir, "gnome")

            #cx_freeze doesn't use "data_files"...
            del setup_options["data_files"]
            #it wants source files first, then where they are placed...
            #one item at a time (no lists)
            #all in its own structure called "include_files" instead of "data_files"...
            def add_data_files(target_dir, files):
                print("add_data_files(%s, %s)" % (target_dir, files))
                assert type(target_dir)==str
                assert type(files) in (list, tuple)
                for f in files:
                    target_file = os.path.join(target_dir, os.path.basename(f))
                    data_files.append((f, target_file))

            #pass a potentially nested dictionary representing the tree
            #of files and directories we do want to include
            #relative to include_dll_path
            def add_dir(base, defs):
                print("add_dir(%s, %s)" % (base, defs))
                if type(defs) in (list, tuple):
                    for sub in defs:
                        if type(sub)==dict:
                            add_dir(base, sub)
                        else:
                            assert type(sub)==str
                            add_data_files(base, [os.path.join(include_dll_path, base, sub)])
                else:
                    assert type(defs)==dict
                    for d, sub in defs.items():
                        assert type(sub) in (dict, list, tuple)
                        #recurse down:
                        add_dir(os.path.join(base, d), sub)

            #convenience method for adding GI libs and "typelib" and "gir":
            def add_gi(*libs):
                print("add_gi(%s)" % str(libs))
                add_dir('lib',      {"girepository-1.0":    ["%s.typelib" % x for x in libs]})
                add_dir('share',    {"gir-1.0" :            ["%s.gir" % x for x in libs]})

            def add_DLLs(*dll_names):
                print("adding DLLs %s" % ", ".join(dll_names))
                dll_names = list(dll_names)
                dll_files = []
                import re
                version_re = re.compile("\-[0-9\.\-]+$")
                for x in os.listdir(include_dll_path):
                    pathname = os.path.join(include_dll_path, x)
                    x = x.lower()
                    if os.path.isdir(pathname) or not x.startswith("lib") or not x.endswith(".dll"):
                        continue
                    nameversion = x[3:-4]                       #strip "lib" and ".dll": "libatk-1.0-0.dll" -> "atk-1.0-0"
                    m = version_re.search(nameversion)          #look for version part of filename
                    if m:
                        dll_version = m.group(0)                #found it, ie: "-1.0-0"
                        dll_name = nameversion[:-len(dll_version)]  #ie: "atk"
                        dll_version = dll_version.lstrip("-")   #ie: "1.0-0"
                    else:
                        dll_version = ""                        #no version
                        dll_name = nameversion                  #ie: "libzzz.dll" -> "zzz"
                    if dll_name in dll_names:
                        #this DLL is on our list
                        print("%s %s %s" % (dll_name.ljust(22), dll_version.ljust(10), x))
                        dll_files.append(x)
                        dll_names.remove(dll_name)
                if len(dll_names)>0:
                    print("some DLLs could not be found in '%s':" % include_dll_path)
                    for x in dll_names:
                        print(" - lib%s*.dll" % x)
                    sys.exit(0)
                add_data_files("", [os.path.join(include_dll_path, dll) for dll in dll_files])


            #list of DLLs we want to include, without the "lib" prefix, or the version and extension
            #(ie: "libatk-1.0-0.dll" -> "atk")
            add_DLLs('atk', 'cairo-gobject',
                     'dbus', 'dbus-glib', 'gdk', 'gdk_pixbuf',
                     'gdkglext', 'gio', 'girepository', 'glib',
                     'gnutls', 'gobject', 'gthread',
                     'gtk', 'gtkglext', 'harfbuzz-gobject',
                     'intl', 'jpeg', 'orc',
                     'p11-kit', 'proxy',
                     'pango', 'pangocairo', 'pangoft2', 'pangowin32',
                     'png16',
                     #ie: libpyglib-gi-2.0-python33
                     'pyglib-gi-2.0-python%s%s' % (sys.version_info[0], sys.version_info[1]),
                     'rsvg', 'webp',
                     'winpthread',
                     'zzz')

            add_dir('etc', ["fonts", "gtk-3.0", "pango", "pkcs11"])     #add "dbus-1"?
            add_dir('lib', ["gdk-pixbuf-2.0", "gio", "gtk-3.0",
                            "libvisual-0.4", "p11-kit", "pkcs11"])
            add_dir('share', ["fontconfig", "fonts", "glib-2.0",        #add "dbus-1"?
                              "icons", "p11-kit", "themes", "xml",
                              {"locale" : ["en"]},
                              {"themes" : ["Default"]}
                             ])
            #FIXME: remove version from those filenames:
            add_gi("Atk-1.0", "cairo-1.0", "fontconfig-2.0",
                   "freetype2-2.0", "GDesktopEnums-3.0",
                   "Gdk-3.0", "GdkGLExt-3.0", "GdkPixbuf-2.0",
                   "Gio-2.0", "GIRepository-2.0",
                   "GL-1.0", "Glib-2.0", "GModule-2.0",
                   "GObject-2.0",
                   "Gtk-3.0", "GtkGLExt-3.0", "HarfBuzz-0.0",
                   "Libproxy-1.0", "libxml2-2.0",
                   "Pango-1.0", "PangoCairo-1.0", "PangoFT2-1.0",
                   "Rsvg-2.0", "win32-1.0")

            if sound_ENABLED:
                add_dir("share", ["gst-plugins-bad", "gst-plugins-base", "gstreamer-1.0"])
                add_gi("Gst-1.0", "GstAllocators-1.0", "GstAudio-1.0", "GstBase-1.0",
                       "GstTag-1.0", "Soup-2.4")
                add_DLLs('curl', 'soup', 'visual',
                         'gstreamer', 'orc-test',
                         'openjpeg',
                         'sqlite3')
                for p in ("app", "audio", "base", "codecparsers", "fft", "net", "video",
                          "pbutils", "riff", "sdp", "rtp", "rtsp", "tag", "uridownloader",
                          #I think 'coreelements' needs those (otherwise we would exclude them):
                          "basecamerabinsrc", "mpegts", "photography",
                          ):
                    add_DLLs('gst%s' % p)
                #add the gstreamer plugins we need:
                GST_PLUGINS = ("app",
                               "audioparsers", "audiorate", "audioconvert", "audioresample", "audiotestsrc",
                               "coreelements", "directsoundsink", "directsoundsrc",
                               "flac", "lame", "mad", "mpg123", "ogg", "speex",
                               "volume", "vorbis", "wavenc", "wavpack", "wavparse",
                               #a52dec, opus, faac, faad, voaacenc
                               )
                add_dir(os.path.join("lib", "gstreamer-1.0"), [("libgst%s.dll" % x) for x in GST_PLUGINS])
                #END OF SOUND

            packages.append("gi")
            #I am reluctant to add these to py2exe because it figures it out already:
            external_includes += ["encodings", "multiprocessing", ]
            #ensure that cx_freeze won't automatically grab other versions that may lay on our path:
            os.environ["PATH"] = include_dll_path+";"+os.environ.get("PATH", "")
            cx_freeze_options = {
                                "compressed"        : False,
                                "includes"          : external_includes,
                                "packages"          : packages,
                                "include_files"     : data_files,
                                "excludes"          : excludes,
                                "include_msvcr"     : True,
                                "create_shared_zip" : True,
                                }
            setup_options["options"] = {"build_exe" : cx_freeze_options}
            executables = []
            setup_options["executables"] = executables

            def add_exe(script, icon, base_name, base="Console"):
                executables.append(Executable(
                            script                  = script,
                            initScript              = None,
                            #targetDir               = "dist",
                            icon                    = "win32/%s" % icon,
                            targetName              = "%s.exe" % base_name,
                            compress                = True,
                            copyDependentFiles      = True,
                            appendScriptToExe       = False,
                            appendScriptToLibrary   = True,
                            base                    = base))

            def add_console_exe(script, icon, base_name):
                add_exe(script, icon, base_name)
            def add_gui_exe(script, icon, base_name):
                add_exe(script, icon, base_name, base="Win32GUI")
            #END OF cx_freeze SECTION
        else:
            import py2exe    #@UnresolvedImport
            assert py2exe is not None
            EXCLUDED_DLLS = list(py2exe.build_exe.EXCLUDED_DLLS) + ["nvcuda.dll"]
            py2exe.build_exe.EXCLUDED_DLLS = EXCLUDED_DLLS
            py2exe_options = {
                              "skip_archive"   : False,
                              "optimize"       : 0,    #WARNING: do not change - causes crashes
                              "unbuffered"     : True,
                              "compressed"     : True,
                              "skip_archive"   : False,
                              "packages"       : packages,
                              "includes"       : external_includes,
                              "excludes"       : excludes,
                              "dll_excludes"   : ["w9xpopen.exe", "tcl85.dll", "tk85.dll"],
                             }
            setup_options["options"] = {"py2exe" : py2exe_options}
            windows = []
            setup_options["windows"] = windows
            console = []
            setup_options["console"] = console

            def add_exe(tolist, script, icon, base_name):
                tolist.append({ 'script'             : script,
                                'icon_resources'    : [(1, "win32/%s" % icon)],
                                "dest_base"         : base_name})
            def add_console_exe(*args):
                add_exe(console, *args)
            def add_gui_exe(*args):
                add_exe(windows, *args)

            # Python2.7 was compiled with Visual Studio 2008:
            # (you can find the DLLs in various packages, including Visual Studio 2008,
            # pywin32, etc...)
            # This is where I keep them, you will obviously need to change this value
            # or make sure you also copy them there:
            C_DLLs = "C:\\"
            check_md5sums({
               C_DLLs+"Microsoft.VC90.CRT/Microsoft.VC90.CRT.manifest"  : "37f44d535dcc8bf7a826dfa4f5fa319b",
               C_DLLs+"Microsoft.VC90.CRT/msvcm90.dll"                  : "4a8bc195abdc93f0db5dab7f5093c52f",
               C_DLLs+"Microsoft.VC90.CRT/msvcp90.dll"                  : "6de5c66e434a9c1729575763d891c6c2",
               C_DLLs+"Microsoft.VC90.CRT/msvcr90.dll"                  : "e7d91d008fe76423962b91c43c88e4eb",
               C_DLLs+"Microsoft.VC90.CRT/vcomp90.dll"                  : "f6a85f3b0e30c96c993c69da6da6079e",
               C_DLLs+"Microsoft.VC90.MFC/Microsoft.VC90.MFC.manifest"  : "17683bda76942b55361049b226324be9",
               C_DLLs+"Microsoft.VC90.MFC/mfc90.dll"                    : "462ddcc5eb88f34aed991416f8e354b2",
               C_DLLs+"Microsoft.VC90.MFC/mfc90u.dll"                   : "b9030d821e099c79de1c9125b790e2da",
               C_DLLs+"Microsoft.VC90.MFC/mfcm90.dll"                   : "d4e7c1546cf3131b7d84b39f8da9e321",
               C_DLLs+"Microsoft.VC90.MFC/mfcm90u.dll"                  : "371226b8346f29011137c7aa9e93f2f6",
               })
            add_data_files('Microsoft.VC90.CRT', glob.glob(C_DLLs+'Microsoft.VC90.CRT\\*.*'))
            add_data_files('Microsoft.VC90.MFC', glob.glob(C_DLLs+'Microsoft.VC90.MFC\\*.*'))
            if (webm_ENABLED or webp_ENABLED):
                #Note: confusingly, the python bindings are called webm...
                #add the webp DLL to the output:
                #And since 0.2.1, you have to compile the DLL yourself..
                #the path after installing may look like this:
                #webp_DLL = "C:\\libwebp-0.3.1-windows-x86\\bin\\libwebp.dll"
                #but we use something more generic, without the version numbers:
                add_data_files('',      [webp_bin_dir+"\\libwebp.dll"])
                add_data_files('webm',  ["xpra/codecs/webm/LICENSE"])
            if enc_x264_ENABLED:
                add_data_files('', ['%s\\libx264.dll' % x264_bin_dir])
                #find pthread DLL...
                for x in (["C:\\MinGW\\bin"]+os.environ.get("PATH").split(";")):
                    f = os.path.join(x, "pthreadGC2.dll")
                    if os.path.exists(f):
                        add_data_files('', [f])
                        break
            #END OF py2exe SECTION

        #UI applications (detached from shell: no text output if ran from cmd.exe)
        add_gui_exe("scripts/xpra",                         "xpra_txt.ico",     "Xpra")
        add_gui_exe("xpra/gtk_common/gtk_view_keyboard.py", "keyboard.ico",     "GTK_Keyboard_Test")
        if not PYTHON3:
            #these need porting..
            add_gui_exe("scripts/xpra_launcher",                "xpra.ico",         "Xpra-Launcher")
            add_gui_exe("xpra/gtk_common/gtk_view_clipboard.py","clipboard.ico",    "GTK_Clipboard_Test")
        #Console: provide an Xpra_cmd.exe we can run from the cmd.exe shell
        add_console_exe("scripts/xpra",                     "xpra_txt.ico",     "Xpra_cmd")
        add_console_exe("xpra/scripts/version.py",          "information.ico",  "Version_info")
        add_console_exe("xpra/net/net_util.py",             "network.ico",      "Network_info")
        add_console_exe("xpra/gtk_common/keymap.py",        "keymap.ico",       "Keymap_info")
        add_console_exe("win32/python_execfile.py",         "python.ico",       "Python_execfile")
        add_console_exe("xpra/codecs/loader.py",            "encoding.ico",     "Encoding_info")
        if sound_ENABLED:
            add_console_exe("xpra/sound/gstreamer_util.py",     "gstreamer.ico",    "GStreamer_info")
            add_console_exe("xpra/sound/src.py",                "microphone.ico",   "Sound_Record")
            add_console_exe("xpra/sound/sink.py",               "speaker.ico",      "Sound_Play")
        if not PYTHON3:
            #these need porting..
            add_console_exe("xpra/platform/win32/gui.py",       "loop.ico",         "Events_Test")
        if opengl_ENABLED:
            add_console_exe("xpra/client/gl/gl_check.py",   "opengl.ico",       "OpenGL_check")


    #always include those files:
    add_data_files('',      ['COPYING', 'README', 'win32/website.url', 'etc/xpra/client-only/xpra.conf'])
    add_data_files('icons', glob.glob('win32\\*.ico') + glob.glob('icons\\*.*'))


    #hard-coded pkgconfig replacement for visual studio:
    #(normally used with python2 / py2exe builds)
    def VC_pkgconfig(*pkgs_options, **ekw):
        kw = dict(ekw)
        #remove static flag on win32..
        for flag in ("static", "ignored_flags"):
            if kw.get(flag) is not None:
                del kw[flag]
        if kw.get("optimize"):
            add_to_keywords(kw, 'extra_compile_args', "/Ox")
            del kw["optimize"]
        #always add the win32 include dirs for VC,
        #so codecs can find the inttypes.h and stdint.h:
        win32_include_dir = os.path.join(os.getcwd(), "win32")
        add_to_keywords(kw, 'include_dirs', win32_include_dir)
        if len(pkgs_options)==0:
            return kw

        def add_to_PATH(*bindirs):
            for bindir in bindirs:
                if os.environ['PATH'].find(bindir)<0:
                    os.environ['PATH'] = bindir + ';' + os.environ['PATH']
                if bindir not in sys.path:
                    sys.path.append(bindir)
        def add_keywords(path_dirs=[], inc_dirs=[], lib_dirs=[], libs=[], noref=True, nocmt=False):
            checkdirs(*path_dirs)
            add_to_PATH(*path_dirs)
            checkdirs(*inc_dirs)
            for d in inc_dirs:
                add_to_keywords(kw, 'include_dirs', d)
            checkdirs(*lib_dirs)
            for d in lib_dirs:
                add_to_keywords(kw, 'extra_link_args', "/LIBPATH:%s" % d)
            add_to_keywords(kw, 'libraries', *libs)
            if noref:
                add_to_keywords(kw, 'extra_link_args', "/OPT:NOREF")
            if nocmt:
                add_to_keywords(kw, 'extra_link_args', "/NODEFAULTLIB:LIBCMT")
        if "avcodec" in pkgs_options[0]:#
            add_keywords([libffmpeg_bin_dir], [libffmpeg_include_dir],
                         [libffmpeg_lib_dir, libffmpeg_bin_dir],
                         ["avcodec", "avutil"])
        elif "swscale" in pkgs_options[0]:
            add_keywords([libffmpeg_bin_dir], [libffmpeg_include_dir],
                         [libffmpeg_lib_dir, libffmpeg_bin_dir],
                         ["swscale", "avutil"])
        elif "x264" in pkgs_options[0]:
            add_keywords([x264_bin_dir], [x264_include_dir],
                         [x264_lib_dir],
                         ["libx264"])
        elif "x265" in pkgs_options[0]:
            add_keywords([x265_bin_dir], [x265_include_dir],
                         [x265_lib_dir],
                         ["libx265"])
        elif "vpx" in pkgs_options[0]:
            add_keywords([vpx_bin_dir], [vpx_include_dir],
                         [vpx_lib_dir],
                         vpx_lib_names, nocmt=True)
        elif "webp" in pkgs_options[0]:
            add_keywords([webp_bin_dir], [webp_include_dir],
                         [webp_lib_dir],
                         webp_lib_names, nocmt=True)
        elif "nvenc3" in pkgs_options[0]:
            add_keywords([nvenc_bin_dir, cuda_bin_dir], [nvenc_include_dir, nvenc_core_include_dir, cuda_include_dir],
                         [cuda_lib_dir],
                         nvenc_lib_names)
            add_data_files('', ["%s/nvcc.exe" % cuda_bin_dir, "%s/nvlink.exe" % cuda_bin_dir])
            #prevent py2exe "seems not to be an exe file" error on this DLL and include it ourselves instead:
            add_data_files('', ["%s/nvcuda.dll" % cuda_bin_dir])
            add_data_files('', ["%s/nvencodeapi.dll" % nvenc_bin_dir])
        elif "pygobject-2.0" in pkgs_options[0]:
            dirs = (python_include_path,
                    pygtk_include_dir, atk_include_dir, gtk2_include_dir,
                    gtk2_base_include_dir, gdkconfig_include_dir, gdkpixbuf_include_dir,
                    glib_include_dir, glibconfig_include_dir,
                    cairo_include_dir, pango_include_dir)
            add_to_keywords(kw, 'include_dirs', *dirs)
            checkdirs(*dirs)
        else:
            sys.exit("ERROR: unknown package config: %s" % str(pkgs_options))
        if debug_ENABLED:
            #Od will override whatever may be specified elsewhere
            #and allows us to use the debug switches,
            #at the cost of a warning...
            for flag in ('/Od', '/Zi', '/DEBUG', '/RTC1', '/GS'):
                add_to_keywords(kw, 'extra_compile_args', flag)
            add_to_keywords(kw, 'extra_link_args', "/DEBUG")
            kw['cython_gdb'] = True
            add_to_keywords(kw, 'extra_compile_args', "/Ox")
        print("pkgconfig(%s,%s)=%s" % (pkgs_options, ekw, kw))
        return kw

    if not has_pkg_config:
        def pkgconfig(*pkgs_options, **ekw):
            #default to VC for now:
            return VC_pkgconfig(*pkgs_options, **ekw)


    remove_packages(*external_excludes)
    remove_packages(#not used on win32:
                    "mmap",
                    #we handle GL separately below:
                    "OpenGL", "OpenGL_accelerate",
                    #this is a mac osx thing:
                    "ctypes.macholib")

    if not cyxor_ENABLED or opengl_ENABLED:
        #we need numpy for opengl or as a fallback for the Cython xor module
        external_includes.append("numpy")
    else:
        remove_packages("numpy",
                        "unittest", "difflib",  #avoid numpy warning (not an error)
                        "pydoc")

    if sound_ENABLED:
        if not PYTHON3:
            external_includes += ["pygst", "gst", "gst.extend"]
            add_data_files('', glob.glob('%s\\bin\\*.dll' % libffmpeg_path))
        else:
            #python3: this is part of "gi"?
            pass
    else:
        remove_packages("pygst", "gst", "gst.extend")

    #deal with opengl workaround (as long as we're not just building the extensions):
    if opengl_ENABLED and "build_ext" not in sys.argv:
        #for this hack to work, you must add "." to the sys.path
        #so python can load OpenGL from the install directory
        #(further complicated by the fact that "." is the "frozen" path...)
        import OpenGL, OpenGL_accelerate        #@UnresolvedImport
        import shutil
        print("*** copy PyOpenGL modules ***")
        for module_name, module in {"OpenGL" : OpenGL, "OpenGL_accelerate" : OpenGL_accelerate}.items():
            module_dir = os.path.dirname(module.__file__ )
            try:
                shutil.copytree(
                    module_dir, os.path.join("dist", module_name),
                    ignore = shutil.ignore_patterns("Tk")
                )
            except:
                e = sys.exc_info()[1]
                if not isinstance(e, WindowsError) or (not "already exists" in str(e)): #@UndefinedVariable
                    raise

    html5_dir = ''

    #END OF win32
#*******************************************************************************
else:
    #OSX and *nix:
    scripts += ["scripts/xpra", "scripts/xpra_launcher"]
    add_data_files("share/man/man1",      ["man/xpra.1", "man/xpra_launcher.1"])
    add_data_files("share/xpra",          ["README", "COPYING"])
    add_data_files("share/xpra/icons",    glob.glob("icons/*"))
    add_data_files("share/applications",  ["xdg/xpra_launcher.desktop", "xdg/xpra.desktop"])
    add_data_files("share/icons",         ["xdg/xpra.png"])
    html5_dir = "share/xpra/www"
    if webm_ENABLED:
        add_data_files('share/xpra/webm', ["xpra/codecs/webm/LICENSE"])

    if OSX:
        #pyobjc needs email.parser
        external_excludes.remove("email")
        external_excludes.remove("urllib")
        external_includes += ["email", "uu", "urllib", "objc"]
        #OSX package names (ie: gdk-x11-2.0 -> gdk-2.0, etc)
        PYGTK_PACKAGES += ["gdk-2.0", "gtk+-2.0"]
        add_packages("xpra.platform.darwin")
    else:
        PYGTK_PACKAGES += ["gdk-x11-2.0", "gtk+-x11-2.0"]
        add_packages("xpra.platform.xposix")
        #always include the wrapper in case we need it later:
        #(we remove it during the 'install' step below if it isn't actually needed)
        scripts.append("scripts/xpra_Xdummy")

    #gentoo does weird things, calls --no-compile with build *and* install
    #then expects to find the cython modules!? ie:
    #> python2.7 setup.py build -b build-2.7 install --no-compile --root=/var/tmp/portage/x11-wm/xpra-0.7.0/temp/images/2.7
    #otherwise we use the flags to skip pkgconfig
    if ("--no-compile" in sys.argv or "--skip-build" in sys.argv) and not ("build" in sys.argv and "install" in sys.argv):
        def pkgconfig(*pkgs_options, **ekw):
            return {}
    if "install" in sys.argv:
        #prepare default [/usr/local]/etc configuration files:
        if sys.prefix == '/usr':
            etc_prefix = '/etc/xpra'
        else:
            etc_prefix = sys.prefix + '/etc/xpra'

        etc_files = []
        if server_ENABLED and x11_ENABLED:
            etc_files = ["etc/xpra/xorg.conf"]
            #figure out the version of the Xorg server:
            xorg_conf, use_Xdummy_wrapper = get_xorg_conf_and_script()
            if not use_Xdummy_wrapper and "scripts/xpra_Xdummy" in scripts:
                #if we're not using the wrapper, don't install it
                scripts.remove("scripts/xpra_Xdummy")
            etc_files.append(xorg_conf)
        add_data_files(etc_prefix, etc_files)

    if OSX and "py2app" in sys.argv:
        import py2app    #@UnresolvedImport
        assert py2app is not None

        #don't use py_modules or scripts with py2app, and no cython:
        del setup_options["py_modules"]
        scripts = []
        def cython_add(*args, **kwargs):
            pass

        remove_packages("ctypes.wintypes", "colorsys")
        remove_packages(*external_excludes)

        Plist = {"CFBundleDocumentTypes" : {
                        "CFBundleTypeExtensions"    : ["Xpra"],
                        "CFBundleTypeName"          : "Xpra Session Config File",
                        "CFBundleName"              : "Xpra",
                        "CFBundleTypeRole"          : "Viewer",
                        }}
        #Note: despite our best efforts, py2app will not copy all the modules we need
        #so the make-app.sh script still has to hack around this problem.
        add_modules(*external_includes)
        py2app_options = {
            'iconfile'          : '../osx/xpra.icns',
            'plist'             : Plist,
            'site_packages'     : False,
            'argv_emulation'    : True,
            'strip'             : False,
            'includes'          : modules,
            'excludes'          : excludes,
            'frameworks'        : ['CoreFoundation', 'Foundation', 'AppKit'],
            }
        setup_options["options"] = {"py2app": py2app_options}
        setup_options["app"]     = ["xpra/client/gtk_base/client_launcher.py"]


if html5_ENABLED:
    for k,v in glob_recurse("html5").items():
        if (k!=""):
            k = os.sep+k
        add_data_files(html5_dir+k, v)



#*******************************************************************************
#which file to link against (new-style buffers or old?):
if memoryview_ENABLED:
    bmod = "new"
else:
    assert not PYTHON3
    bmod = "old"
buffers_c = "xpra/codecs/buffers/%s_buffers.c" % bmod
#convenience grouping for codecs:
membuffers_c = ["xpra/codecs/buffers/memalign.c", "xpra/codecs/inline.c", buffers_c]


toggle_packages(server_ENABLED, "xpra.server", "xpra.server.stats", "xpra.server.auth")
toggle_packages(server_ENABLED or gtk2_ENABLED or gtk3_ENABLED, "xpra.gtk_common", "xpra.clipboard")


toggle_packages(x11_ENABLED, "xpra.x11", "xpra.x11.bindings")
if x11_ENABLED:
    make_constants("xpra", "x11", "bindings", "constants")
    make_constants("xpra", "x11", "gtk_x11", "constants")

    cython_add(Extension("xpra.x11.bindings.wait_for_x_server",
                ["xpra/x11/bindings/wait_for_x_server.pyx"],
                **pkgconfig("x11")
                ))
    cython_add(Extension("xpra.x11.bindings.display_source",
                ["xpra/x11/bindings/display_source.pyx"],
                **pkgconfig("x11")
                ))
    cython_add(Extension("xpra.x11.bindings.core_bindings",
                ["xpra/x11/bindings/core_bindings.pyx"],
                **pkgconfig("x11")
                ))
    cython_add(Extension("xpra.x11.bindings.randr_bindings",
                ["xpra/x11/bindings/randr_bindings.pyx"],
                **pkgconfig("x11", "xrandr")
                ))
    cython_add(Extension("xpra.x11.bindings.keyboard_bindings",
                ["xpra/x11/bindings/keyboard_bindings.pyx"],
                **pkgconfig("x11", "xtst", "xfixes")
                ))

    cython_add(Extension("xpra.x11.bindings.window_bindings",
                ["xpra/x11/bindings/window_bindings.pyx"],
                **pkgconfig("xtst", "xfixes", "xcomposite", "xdamage")
                ))
    cython_add(Extension("xpra.x11.bindings.ximage",
                ["xpra/x11/bindings/ximage.pyx", buffers_c],
                **pkgconfig("xcomposite", "xdamage", "xext")
                ))

toggle_packages(gtk_x11_ENABLED, "xpra.x11.gtk_x11")
if gtk_x11_ENABLED:
    if PYTHON3:
        #GTK3 display source:
        cython_add(Extension("xpra.x11.gtk3_x11.gdk_display_source",
                    ["xpra/x11/gtk3_x11/gdk_display_source.pyx"],
                    **pkgconfig("gtk+-3.0")
                    ))
    else:
        #below uses gtk/gdk:
        cython_add(Extension("xpra.x11.gtk_x11.gdk_display_source",
                    ["xpra/x11/gtk_x11/gdk_display_source.pyx"],
                    **pkgconfig(*PYGTK_PACKAGES)
                    ))
        GDK_BINDINGS_PACKAGES = PYGTK_PACKAGES + ["xfixes", "xdamage"]
        cython_add(Extension("xpra.x11.gtk_x11.gdk_bindings",
                    ["xpra/x11/gtk_x11/gdk_bindings.pyx"],
                    **pkgconfig(*GDK_BINDINGS_PACKAGES)
                    ))


toggle_packages(argb_ENABLED, "xpra.codecs.argb")
if argb_ENABLED:
    cython_add(Extension("xpra.codecs.argb.argb",
                ["xpra/codecs/argb/argb.pyx", buffers_c]))


if bundle_tests_ENABLED:
    #bundle the tests directly (not in library.zip):
    for k,v in glob_recurse("tests").items():
        if (k!=""):
            k = os.sep+k
        add_data_files("tests"+k, v)

#special case for client: cannot use toggle_packages which would include gtk3, qt, etc:
if client_ENABLED:
    add_modules("xpra.client", "xpra.client.notifications")
toggle_packages((client_ENABLED and (gtk2_ENABLED or gtk3_ENABLED)) or server_ENABLED, "xpra.gtk_common")
toggle_packages(client_ENABLED and gtk2_ENABLED, "xpra.client.gtk2")
toggle_packages(client_ENABLED and gtk3_ENABLED, "xpra.client.gtk3", "gi")
toggle_packages(client_ENABLED and qt4_ENABLED, "xpra.client.qt4", "PyQt4")
toggle_packages(client_ENABLED and (gtk2_ENABLED or gtk3_ENABLED), "xpra.client.gtk_base")
toggle_packages(sound_ENABLED, "xpra.sound")
toggle_packages(webm_ENABLED, "xpra.codecs.webm")
toggle_packages(client_ENABLED and gtk2_ENABLED and opengl_ENABLED, "xpra.client.gl")

toggle_packages(clipboard_ENABLED, "xpra.clipboard")
if clipboard_ENABLED:
    cython_add(Extension("xpra.gtk_common.gdk_atoms",
                ["xpra/gtk_common/gdk_atoms.pyx"],
                **pkgconfig(*PYGTK_PACKAGES)
                ))

if cyxor_ENABLED:
    cython_add(Extension("xpra.codecs.xor.cyxor",
                ["xpra/codecs/xor/cyxor.pyx", buffers_c],
                **pkgconfig()))

if cymaths_ENABLED:
    cython_add(Extension("xpra.server.stats.cymaths",
                ["xpra/server/stats/cymaths.pyx"],
                **pkgconfig()))


toggle_packages(csc_opencl_ENABLED, "xpra.codecs.csc_opencl")
toggle_packages(enc_proxy_ENABLED, "xpra.codecs.enc_proxy")

toggle_packages(nvenc_ENABLED, "xpra.codecs.nvenc", "xpra.codecs.cuda_common")
if nvenc_ENABLED:
    make_constants("xpra", "codecs", "nvenc", "constants", NV_WINDOWS=int(sys.platform.startswith("win")))
    nvenc_pkgconfig = pkgconfig("nvenc3", "cuda", ignored_flags=["-l", "-L"])
    #don't link against libnvidia-encode, we load it dynamically:
    libraries = nvenc_pkgconfig.get("libraries", [])
    if "nvidia-encode" in libraries:
        libraries.remove("nvidia-encode")
    cython_add(Extension("xpra.codecs.nvenc.encoder",
                         ["xpra/codecs/nvenc/encoder.pyx", buffers_c],
                         **nvenc_pkgconfig))

toggle_packages(enc_x264_ENABLED, "xpra.codecs.enc_x264")
if enc_x264_ENABLED:
    x264_pkgconfig = pkgconfig("x264", static=x264_static_ENABLED)
    cython_add(Extension("xpra.codecs.enc_x264.encoder",
                ["xpra/codecs/enc_x264/encoder.pyx", buffers_c],
                **x264_pkgconfig))

toggle_packages(enc_x265_ENABLED, "xpra.codecs.enc_x265")
if enc_x265_ENABLED:
    x265_pkgconfig = pkgconfig("x265", static=x265_static_ENABLED)
    cython_add(Extension("xpra.codecs.enc_x265.encoder",
                ["xpra/codecs/enc_x265/encoder.pyx", buffers_c],
                **x265_pkgconfig))

toggle_packages(webp_ENABLED, "xpra.codecs.webp")
if webp_ENABLED:
    webp_pkgconfig = pkgconfig("webp")
    cython_add(Extension("xpra.codecs.webp.encode",
                ["xpra/codecs/webp/encode.pyx", buffers_c],
                **webp_pkgconfig))
    cython_add(Extension("xpra.codecs.webp.decode",
                ["xpra/codecs/webp/decode.pyx"]+membuffers_c,
                **webp_pkgconfig))

toggle_packages(dec_avcodec_ENABLED, "xpra.codecs.dec_avcodec")
if dec_avcodec_ENABLED:
    make_constants("xpra", "codecs", "dec_avcodec", "constants")
    avcodec_pkgconfig = pkgconfig("avcodec", "avutil", static=avcodec_static_ENABLED)
    cython_add(Extension("xpra.codecs.dec_avcodec.decoder",
                ["xpra/codecs/dec_avcodec/decoder.pyx"]+membuffers_c,
                **avcodec_pkgconfig))

toggle_packages(dec_avcodec2_ENABLED, "xpra.codecs.dec_avcodec2")
if dec_avcodec2_ENABLED:
    avcodec2_pkgconfig = pkgconfig("avcodec", "avutil", static=avcodec2_static_ENABLED)
    cython_add(Extension("xpra.codecs.dec_avcodec2.decoder",
                ["xpra/codecs/dec_avcodec2/decoder.pyx"]+membuffers_c,
                **avcodec2_pkgconfig))


toggle_packages(csc_swscale_ENABLED, "xpra.codecs.csc_swscale")
if csc_swscale_ENABLED:
    make_constants("xpra", "codecs", "csc_swscale", "constants")
    swscale_pkgconfig = pkgconfig("swscale", "avutil", static=swscale_static_ENABLED)
    cython_add(Extension("xpra.codecs.csc_swscale.colorspace_converter",
                ["xpra/codecs/csc_swscale/colorspace_converter.pyx"]+membuffers_c,
                **swscale_pkgconfig))

toggle_packages(csc_cython_ENABLED, "xpra.codecs.csc_cython")
if csc_cython_ENABLED:
    csc_cython_pkgconfig = pkgconfig()
    cython_add(Extension("xpra.codecs.csc_cython.colorspace_converter",
                ["xpra/codecs/csc_cython/colorspace_converter.pyx"]+membuffers_c,
                **csc_cython_pkgconfig))

toggle_packages(vpx_ENABLED, "xpra.codecs.vpx")
if vpx_ENABLED:
    vpx_pkgconfig = pkgconfig("vpx", static=vpx_static_ENABLED)
    cython_add(Extension("xpra.codecs.vpx.encoder",
                ["xpra/codecs/vpx/encoder.pyx"]+membuffers_c,
                **vpx_pkgconfig))
    cython_add(Extension("xpra.codecs.vpx.decoder",
                ["xpra/codecs/vpx/decoder.pyx"]+membuffers_c,
                **vpx_pkgconfig))


toggle_packages(rencode_ENABLED, "xpra.net.rencode")
if rencode_ENABLED:
    rencode_pkgconfig = pkgconfig(optimize=not debug_ENABLED)
    cython_add(Extension("xpra.net.rencode.rencode",
                ["xpra/net/rencode/rencode.pyx"],
                **rencode_pkgconfig))


toggle_packages(bencode_ENABLED, "xpra.net.bencode")
if cython_bencode_ENABLED:
    bencode_pkgconfig = pkgconfig(optimize=not debug_ENABLED)
    cython_add(Extension("xpra.net.bencode.cython_bencode",
                ["xpra/net/bencode/cython_bencode.pyx"],
                **bencode_pkgconfig))


if ext_modules:
    from Cython.Build import cythonize
    #this causes Cython to fall over itself:
    #gdb_debug=debug_ENABLED
    setup_options["ext_modules"] = cythonize(ext_modules, gdb_debug=False)
if cmdclass:
    setup_options["cmdclass"] = cmdclass
if scripts:
    setup_options["scripts"] = scripts


def main():
    if OSX or WIN32 or debug_ENABLED:
        print("setup options:")
        print_dict(setup_options)
        print("")

    setup(**setup_options)


if __name__ == "__main__":
    main()
