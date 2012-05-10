#!/usr/bin/env python
# This file is part of Parti.
# Copyright (C) 2012 Antoine Martin <antoine@devloop.org.uk>
# Parti is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import subprocess
import os.path
import time

from wimpiggy.log import Logger
log = Logger()
HOME = os.path.expanduser("~/")

#You will probably need to change those:
IP = "192.168.42.100"       #this is your IP
PORT = 10000                #the port to test on
DISPLAY_NO = 10             #the test DISPLAY no to use
XORG_CONFIG="%s/xorg.conf" % HOME
XORG_LOG = "%s/Xorg.%s.log" % (HOME, DISPLAY_NO)
START_SERVER = True         #if False, you are responsible for starting it
                            #and the data will not be available
TEST_XPRA = True
TEST_VNC = True

LIMIT_TESTS = 8
LIMIT_TESTS = 99999

TRICKLE_SHAPING_OPTIONS = [(1024, 1024, 20), (128, 32, 40), (0, 0, 0)]

TRICKLE_BIN = "/usr/bin/trickle"
GLX_SPHERES = ["/opt/VirtualGL/bin/glxspheres"]
#GLX_GEARS = ["/usr/bin/glxgears", "-geometry", "1240x900"]
X11_PERF = ["/usr/bin/x11perf", "-resize", "-all"]
XTERM_TEST = ["/usr/bin/xterm", "-geometry", "160x60", "-e", "while true; do dmesg; done"]
GTKPERF_TEST = "while true; do gtkperf -a; done"
XSCREENSAVERS_PATH = "/usr/libexec/xscreensaver"
ALL_XSCREENSAVER_TESTS = ["%s/%s" % (XSCREENSAVERS_PATH, x) for x in
                        ["rss-glx-hufo_tunnel", "rss-glx-lattice", "rss-glx-plasma", "deluxe", "eruption", "memscroller", "moebiusgears", "polytopes", "rss-glx-drempels",
                         "xjack", "xmatrix"]
                          ]
SOME_XSCREENSAVER_TESTS = [["%s/%s" % (XSCREENSAVERS_PATH, x)] for x in
                        ["rss-glx-hufo_tunnel", "memscroller"]
                          ]
X11_TEST_COMMANDS = []
for x in [GLX_SPHERES, X11_PERF, XTERM_TEST, GTKPERF_TEST] + SOME_XSCREENSAVER_TESTS:
    if not os.path.exists(x[0]):
        print("WARNING: cannot find %s - removed from tests" % str(x))
    else:
        X11_TEST_COMMANDS.append(x)


#but these should be ok:
SETTLE_TIME = 3             #how long to wait before we start measuring
MEASURE_TIME = 40           #run for N seconds
SERVER_SETTLE_TIME = 3      #how long we wait for the server to start
TEST_COMMAND_SETTLE_TIME = 1    #how long we wait after starting the test command

XVNC_BIN = "/usr/bin/Xvnc"
XVNC_SERVER_START_COMMAND = [XVNC_BIN, "--rfbport=%s" % PORT,
                   "+extension", "GLX",
                   "--SecurityTypes=None",
                   "--SendCutText=0", "--AcceptCutText=0", "--AcceptPointerEvents=0", "--AcceptKeyEvents=0",
                   "-screen", "0", "1240x900x24",
                   ":%s" % DISPLAY_NO]
XVNC_SERVER_STOP_COMMANDS = [["killall Xvnc"]]     #stopped via kill - beware, this will kill *all* Xvnc sessions!
VNCVIEWER_BIN = "/usr/bin/vncviewer"
VNC_ENCODINGS = ["Tight", "ZRLE", "hextile", "raw", "auto"]
VNC_ZLIB_OPTIONS = [-1, 3, 6, 9]
VNC_ZLIB_OPTIONS = [-1, 9]
VNC_COMPRESSION_OPTIONS = [0, 3, 8, 9]
VNC_COMPRESSION_OPTIONS = [0, 3]
VNC_JPEG_OPTIONS = [-1, 0, 8]
VNC_JPEG_OPTIONS = [-1, 4]



XPRA_BIN = "/usr/bin/xpra"
XPRA_SERVER_START_COMMAND = [XPRA_BIN, "--no-daemon", "--bind-tcp=0.0.0.0:%s" % PORT,
                       "start", ":%s" % DISPLAY_NO,
                       "--xvfb=Xorg -nolisten tcp +extension GLX +extension RANDR +extension RENDER -logfile %s -config %s" % (XORG_LOG, XORG_CONFIG)]
XPRA_SERVER_STOP_COMMANDS = [
                             [XPRA_BIN, "stop", ":%s" % DISPLAY_NO],
                             "ps -ef | grep -i [X]org-for-Xpra-:%s | awk '{print $2}' | xargs kill" % DISPLAY_NO
                             ]
XPRA_INFO_COMMAND = [XPRA_BIN, "info", ":%s" % DISPLAY_NO]
XPRA_TEST_ENCODINGS = ["png", "rgb24", "jpeg", "x264", "vpx", "mmap"]
XPRA_JPEG_OPTIONS = [40, 90]
XPRA_JPEG_OPTIONS = [40, 80, 90]
XPRA_COMPRESSION_OPTIONS = [0, 3, 9]
XPRA_COMPRESSION_OPTIONS = [0, 3]


check = [TRICKLE_BIN]
if TEST_XPRA:
    check.append(XPRA_BIN)
if TEST_VNC:
    check.append(XVNC_BIN)
    check.append(VNCVIEWER_BIN)
for x in check:
    if not os.path.exists(x):
        raise Exception("cannot run tests: %s is missing!" % x)

def try_to_stop(process):
    if not process or process.poll() is not None:
        return
    try:
        process.terminate()
    except Exception, e:
        print("could not stop process %s: %s" % (process, e))
def try_to_kill(process):
    if not process or process.poll() is not None:
        return
    try:
        process.kill()
    except Exception, e:
        print("could not stop process %s: %s" % (process, e))

def getoutput(cmd):
    try:
        process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except Exception, e:
        print("error running %s: %s" % (cmd, e))
        raise e
    (out,_) = process.communicate()
    code = process.poll()
    if code!=0:
        raise Exception("command '%s' returned error code %s" % (cmd, code))
    return out

def getoutput_lines(cmd, pattern, setup_info):
    out = getoutput(cmd)
    lines = []
    for line in out.splitlines():
        if line.find(pattern)>=0:
            lines.append(line)
    return  lines
def getoutput_line(cmd, pattern, setup_info):
    lines = getoutput_lines(cmd, pattern, setup_info)
    assert len(lines)==1, "expected 1 line matching '%s' from %s but found %s" % (pattern, cmd, len(lines))
    return  lines[0]

def get_cpu_info():
    lines = getoutput_lines(["cat", "/proc/cpuinfo"], "model name", "cannot find cpu info")
    assert len(lines)>0, "coult not find 'model name' in '/proc/cpuinfo'"
    cpu0 = lines[0]
    n = len(lines)
    for x in lines[1:]:
        if x!=cpu0:
            return " - ".join(lines), n
    cpu_name = cpu0.split(":")[1]
    for o,r in [("Processor", ""), ("(R)", ""), ("(TM)", ""), ("(tm)", ""), ("  ", " ")]:
        while cpu_name.find(o)>=0:
            cpu_name = cpu_name.replace(o, r)
    cpu_info = "%sx %s" % (len(lines), cpu_name.strip())
    print("CPU_INFO=%s" % cpu_info)
    return  cpu_info, n

XORG_VERSION = getoutput_line(["Xorg", "-version"], "X.Org X Server", "Cannot detect Xorg server version")
print("XORG_VERSION=%s" % XORG_VERSION)
CPU_INFO, N_CPUS = get_cpu_info()
PAGE_SIZE = int(getoutput(["getconf", "PAGESIZE"]).replace("\n", "").replace("\r", ""))

def clean_sys_state():
    #clear the caches
    cmd = ["echo", "3", ">", "/proc/sys/vm/drop_caches"]
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.wait()==0, "failed to run %s" % str(cmd)

def zero_iptables():
    cmds = [['iptables', '-Z', 'INPUT'], ['iptables', '-Z', 'OUTPUT']]
    for cmd in cmds:
        getoutput(cmd)
        #out = getoutput(cmd)
        #print("output(%s)=%s" % (cmd, out))

def update_proc_stat():
    proc_stat = open("/proc/stat", "rU")
    time_total = 0
    for line in proc_stat:
        values = line.split()
        if values[0]=="cpu":
            time_total = sum([int(x) for x in values[1:]])
            #print("time_total=%s" % time_total)
            break
    proc_stat.close()
    return time_total

def update_pidstat(pid):
    stat_file = open("/proc/%s/stat" % pid, "rU")
    data = stat_file.read()
    stat_file.close()
    pid_stat = data.split()
    #print("update_pidstat(%s): %s" % (pid, pid_stat))
    return pid_stat

def compute_stat(time_total_diff, old_pid_stat, new_pid_stat):
    #found help here:
    #http://stackoverflow.com/questions/1420426/calculating-cpu-usage-of-a-process-in-linux
    old_utime = int(old_pid_stat[13])
    old_stime = int(old_pid_stat[14])
    new_utime = int(new_pid_stat[13])
    new_stime = int(new_pid_stat[14])
    #normalize to 100% (single process) by multiplying by number of CPUs:
    user_pct = int(N_CPUS * 1000 * (new_utime - old_utime) / time_total_diff)/10.0
    sys_pct = int(N_CPUS * 1000 * (new_stime - old_stime) / time_total_diff)/10.0
    nthreads = int((int(old_pid_stat[19])+int(new_pid_stat[19]))/2)
    vsize = int(max(int(old_pid_stat[22]), int(new_pid_stat[22]))/1024/1024)
    rss = int(max(int(old_pid_stat[23]), int(new_pid_stat[23]))*PAGE_SIZE/1024/1024)
    return [user_pct, sys_pct, nthreads, vsize, rss]

def getiptables_line(chain, pattern, setup_info):
    cmd = ["iptables", "-vnL", chain]
    line = getoutput_line(cmd, pattern, setup_info)
    if not line:
        raise Exception("no line found matching %s, make sure you have a rule like: %s" % (pattern, setup_info))
    return line

def parse_ipt(chain, pattern, setup_info):
    line = getiptables_line(chain, pattern, setup_info)
    parts = line.split()
    assert len(parts)>2
    def parse_num(part):
        U = 1024
        m = {"K":U, "M":U**2, "G":U**3}.get(part[-1], 1)
        num = "".join([x for x in part if x in "0123456789"])
        return int(num)*m
    return parse_num(parts[0]), parse_num(parts[1])

def get_input_count():
    setup = "iptables -I INPUT -p tcp --dport %s -j ACCEPT" % PORT
    return  parse_ipt("INPUT", "tcp dpt:%s" % PORT, setup)

def get_output_count():
    setup = "iptables -I OUTPUT -p tcp --sport %s -j ACCEPT" % PORT
    return  parse_ipt("OUTPUT", "tcp spt:%s" % PORT, setup)

def measure_client(server_pid, name, cmd, get_stats_cb=None):
    print("starting client: %s" % cmd)
    client_process = subprocess.Popen(cmd)
    #give it time to settle down:
    time.sleep(SETTLE_TIME)
    code = client_process.poll()
    assert code is None, "client failed to start, return code is %s" % code
    #clear counters
    if get_stats_cb:
        initial_stats = get_stats_cb()
    zero_iptables()
    old_time_total = update_proc_stat()
    old_pid_stat = update_pidstat(client_process.pid)
    if server_pid>0:
        old_server_pid_stat = update_pidstat(server_pid)
    #we start measuring
    time.sleep(MEASURE_TIME)
    code = client_process.poll()
    assert code is None, "client crashed, return code is %s" % code
    #stop the counters
    new_time_total = update_proc_stat()
    new_pid_stat = update_pidstat(client_process.pid)
    if server_pid>0:
        new_server_pid_stat = update_pidstat(server_pid)
    ni,isize = get_input_count()
    no,osize = get_output_count()
    if get_stats_cb and initial_stats:
        stats = get_stats_cb(initial_stats)
    else:
        stats = get_empty_stats()
    #stop the process
    try_to_stop(client_process)
    time.sleep(1)
    try_to_kill(client_process)
    code = client_process.poll()
    assert code is not None, "failed to stop client!"
    #now collect the data
    client_process_data = compute_stat(new_time_total-old_time_total, old_pid_stat, new_pid_stat)
    if server_pid>0:
        server_process_data = compute_stat(new_time_total-old_time_total, old_server_pid_stat, new_server_pid_stat)
    else:
        server_process_data = []
    print("process_data (client/server): %s / %s" % (client_process_data, server_process_data))
    print("input/output on tcp port %s: %s / %s packets, %s / %s KBytes" % (PORT, ni, no, isize, osize))
    return [ni, isize, no, osize] + stats + client_process_data + server_process_data

def with_server(start_server_command, stop_server_commands, in_tests, get_stats_cb=None):
    tests = in_tests[-LIMIT_TESTS:]
    print("going to run %s tests: %s" % (len(tests), [x[0] for x in tests]))
    print("ETA: %s minutes" % int((SERVER_SETTLE_TIME+TEST_COMMAND_SETTLE_TIME+SETTLE_TIME+MEASURE_TIME+1)*len(tests)/60))

    server_process = None
    test_command_process = None
    env = os.environ.copy()
    env["DISPLAY"] = ":%s" % DISPLAY_NO
    errors = 0
    results = []
    count = 0
    for name, tech_name, encoding, compression, (down,up,latency), test_command, client_cmd in tests:
        try:
            print("**************************************************************")
            count += 1
            eta = int((SERVER_SETTLE_TIME+TEST_COMMAND_SETTLE_TIME+SETTLE_TIME+MEASURE_TIME+1)*(len(tests)-count)/60)
            print("%s/%s: %s            ETA=%s minutes" % (count, len(tests), name, eta))
            test_command_process = None
            try:
                clean_sys_state()
                #start the server:
                if START_SERVER:
                    print("starting server: %s" % str(start_server_command))
                    server_process = subprocess.Popen(start_server_command, stdin=None)
                    #give it time to settle down:
                    time.sleep(SERVER_SETTLE_TIME)
                    server_pid = server_process.pid
                    code = server_process.poll()
                    assert code is None, "server failed to start, return code is %s, please ensure that you can run the server command line above and that a server does not already exist on that port or DISPLAY" % code
                else:
                    server_pid = 0
    
                #start the test command:
                print("starting test command: %s" % str(test_command))
                test_command_process = subprocess.Popen(test_command, stdin=None, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, shell=type(test_command)==str)
                time.sleep(TEST_COMMAND_SETTLE_TIME)
                code = test_command_process.poll()
                assert code is None, "test command %s failed to start: exit code is %s" % (test_command, code)
    
                #run the client test
                result = [name, tech_name, encoding, get_command_name(test_command), MEASURE_TIME, CPU_INFO, XORG_VERSION, compression, down, up, latency]
                result += measure_client(server_pid, name, client_cmd, get_stats_cb)
                results.append(result)
            except Exception, e:
                import traceback
                traceback.print_exc()
                errors += 1
                print("error during client command run for %s: %s" % (name, e))
                if errors>10:
                    print("too many errors, aborting tests")
                    break
            finally:
                if test_command_process:
                    print("stopping '%s' with pid=%s" % (test_command, test_command_process.pid))
                    try_to_stop(test_command_process)
                    time.sleep(0.5)
                    try_to_kill(test_command_process)
                if START_SERVER:
                    try_to_stop(server_process)
                    time.sleep(0.5)
                    for s in stop_server_commands:
                        print("stopping server with: %s" % (s))
                        try:
                            stop_process = subprocess.Popen(s, stdin=None, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
                            stop_process.wait()
                        except Exception, e:
                            print("error: %s" % e)
                    try_to_kill(server_process)
                time.sleep(1)
        except KeyboardInterrupt, e:
            print("caught %s: stopping this series of tests" % e)
            break
    return results


def trickle_command(down, up, latency):
    if down<=0 and up<=0 and latency<=0:
        return  []
    cmd = [TRICKLE_BIN, "-s"]
    if down>0:
        cmd += ["-d", str(down)]
    if up>0:
        cmd += ["-u", str(up)]
    if latency>0:
        cmd += ["-L", str(latency)]
    return cmd

def trickle_str(down, up, latency):
    if down<=0 and up<=0 and latency<=0:
        return  "unthrottled"
    s = "/".join(str(x) for x in [down,up,latency])
    return "throttled:%s" % s

def get_command_name(command_arg):
    if type(command_arg)==list:
        c = command_arg[0]              #["/usr/bin/xterm", "blah"] -> "/usr/bin/xterm"
    else:
        c = command_arg.split(" ")[0]   #"/usr/bin/xterm -e blah" -> "/usr/bin/xterm"
    assert type(c)==str
    return c.split("/")[-1]             #/usr/bin/xterm -> xterm

def xpra_get_stats(last_record=None):
    out = getoutput(XPRA_INFO_COMMAND)
    if not out:
        return  None
    d = {}
    for line in out.splitlines():
        parts = line.split("=")
        if len(parts)==2:
            d[parts[0]] = parts[1]
    last_input_packetcount = 0
    last_output_packetcount = 0
    if last_record:
        last_input_packetcount = last_record[3] or 0
        last_output_packetcount = last_record[4] or 0
    return [
            d.get("regions_per_second", ""),
            d.get("pixels_per_second", ""),
            d.get("pixels_decoded_per_second", ""),
            int(d.get("input_packetcount", 0))-last_input_packetcount,
            int(d.get("output_packetcount", 0))-last_output_packetcount,
            d.get("min_client_latency", ""),
            d.get("max_client_latency", ""),
            d.get("avg_client_latency", ""),
            d.get("min_server_latency", ""),
            d.get("max_server_latency", ""),
            d.get("avg_server_latency", ""),
           ]
def get_empty_stats():
    return  ["", "", "",
             "", "",
             "", "", "",
             "", "", ""
             ]

def get_xpra_stats_headers():
    return  ["Regions/s", "Pixels/s", "Decoding Pixels/s",
             "application packets in", "application packets out",
             "Min Client Latency", "Max Client Latency", "Avg Client Latency",
             "Min Server Latency", "Max Server Latency", "Avg Server Latency",
             ]

def test_xpra():
    print("")
    print("*********************************************************")
    print("                Xpra tests")
    print("")
    tests = []
    for down,up,latency in TRICKLE_SHAPING_OPTIONS:
        for x11_test_command in X11_TEST_COMMANDS:
            for encoding in XPRA_TEST_ENCODINGS:
                QUALITY = [-1]
                if encoding=="jpeg":
                    QUALITY = XPRA_JPEG_OPTIONS
                for jpeg_q in QUALITY:
                    comp_options = XPRA_COMPRESSION_OPTIONS
                    for compression in comp_options:
                        cmd = trickle_command(down, up, latency)
                        cmd += [XPRA_BIN,
                               "attach", "tcp:%s:%s" % (IP, PORT),
                               "--enable-pings",
                               "-z", str(compression), "--readonly"]
                        if encoding=="jpeg":
                            cmd.append("--jpeg-quality=%s" % jpeg_q)
                            name = "%s-%s" % (encoding, jpeg_q)
                        else:
                            name = encoding
                        if encoding!="mmap":
                            cmd.append("--no-mmap")
                            cmd.append("--encoding=%s" % encoding)
                        command_name = get_command_name(x11_test_command)
                        test_name = "%s (%s - %s - %s)" % (name, command_name, compression, trickle_str(down, up, latency))
                        tests.append((test_name, "xpra", encoding, compression, (down,up,latency), x11_test_command, cmd))
    return with_server(XPRA_SERVER_START_COMMAND, XPRA_SERVER_STOP_COMMANDS, tests, xpra_get_stats)

def test_vnc():
    print("")
    print("*********************************************************")
    print("                VNC tests")
    print("")
    tests = []
    for down,up,latency in TRICKLE_SHAPING_OPTIONS:
        for x11_test_command in X11_TEST_COMMANDS:
            for encoding in VNC_ENCODINGS:
                for zlib in VNC_ZLIB_OPTIONS:
                    for compression in VNC_COMPRESSION_OPTIONS:
                        jpeg_quality = [8]
                        if encoding=="Tight":
                            jpeg_quality = VNC_JPEG_OPTIONS
                        for jpegq in jpeg_quality:
                            cmd = trickle_command(down, up, latency)
                            cmd += [VNCVIEWER_BIN, "%s::%s" % (IP, PORT),
                                   "--ViewOnly",
                                   "--ZlibLevel=%s" % str(zlib),
                                   "--CompressLevel=%s" % str(compression),
                                   ]
                            if encoding=="auto":
                                cmd.append("--AutoSelect=1")
                            else:
                                cmd.append("--AutoSelect=0")
                                cmd.append("--PreferredEncoding=%s" % encoding)
                            if jpegq<0:
                                cmd.append("--NoJPEG=1")
                                jpegtxt = "nojpeg"
                            else:
                                cmd.append("--NoJPEG=0")
                                cmd.append("--QualityLevel=%s" % jpegq)
                                jpegtxt = "jpeg=%s" % jpegq
                            #make a descriptive title:
                            if zlib==-1:
                                zlibtxt = "nozlib"
                            else:
                                zlibtxt = "zlib=%s" % zlib
                            command_name = get_command_name(x11_test_command)
                            test_name = "vnc (%s - %s - %s - compression=%s - %s - %s)" % (command_name, encoding, zlibtxt, compression, jpegtxt, trickle_str(down, up, latency))
                            tests.append((test_name, "vnc", encoding, compression, (down,up,latency), x11_test_command, cmd))
    return with_server(XVNC_SERVER_START_COMMAND, XVNC_SERVER_STOP_COMMANDS, tests, None)

def main():
    #before doing anything, check that the firewall is setup correctly:
    get_input_count()
    get_output_count()

    xpra_results = []
    if TEST_XPRA:
        xpra_results = test_xpra()
    vnc_results = []
    if TEST_VNC:
        vnc_results = test_vnc()
    print("")
    print("results:")
    headers = ["Test Name", "Remoting Tech", "Encoding", "Test Command", "Sample Duration",
               "CPU info", "Xorg version", "compression", "download limit (KB)", "upload limit (KB)", "latency (ms)",
               "packets in", "packets in volume", "packets out", "packets out volume"]
    headers += get_xpra_stats_headers()
    headers += ["client user cpu_pct", "client system cpu pct", "client number of threads", "client vsize (MB)", "client rss (MB)",
               "server user cpu_pct", "server system cpu pct", "server number of threads", "server vsize (MB)", "server rss (MB)",
               ]
    print(", ".join(headers))
    for result in xpra_results+vnc_results:
        print ", ".join([str(x) for x in result])

if __name__ == "__main__":
    main()
