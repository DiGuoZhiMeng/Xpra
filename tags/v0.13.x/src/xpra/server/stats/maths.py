# coding=utf8
# This file is part of Xpra.
# Copyright (C) 2012, 2013 Antoine Martin <antoine@devloop.org.uk>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

# Math functions used for inspecting/averaging lists of statistics
# see ServerSource, WindowSource and batch_delay_calculator
# We load them from cymaths and fallback to pymaths

has_cymaths = False
try:
    import os
    if os.environ.get("XPRA_CYTHON_MATH", "1")=="1":
        from xpra.server.stats.cymaths import (logp,                       #@UnresolvedImport @UnusedImport
                              calculate_time_weighted_average,      #@UnresolvedImport @UnusedImport
                              time_weighted_average,                #@UnresolvedImport @UnusedImport
                              calculate_timesize_weighted_average,  #@UnresolvedImport @UnusedImport
                              calculate_for_target,                 #@UnresolvedImport @UnusedImport
                              calculate_for_average, queue_inspect) #@UnresolvedImport @UnusedImport
        has_cymaths = True
except ImportError:
    pass

if not has_cymaths:
    from xpra.server.stats.pymaths import (logp,                           #@UnusedImport @Reimport
                              calculate_time_weighted_average,      #@UnusedImport @Reimport
                              time_weighted_average,                #@UnusedImport @Reimport
                              calculate_timesize_weighted_average,  #@UnusedImport @Reimport
                              calculate_for_target,                 #@UnusedImport @Reimport
                              calculate_for_average, queue_inspect) #@UnusedImport @Reimport
