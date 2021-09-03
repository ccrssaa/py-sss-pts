#!/usr/bin/env python3
"""
    Copyright (C) 2021  Vadim Ponomarev <vadim@cs.petrsu.ru>

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""

import argparse
import logging
import sys
import os
import subprocess
import json
import re

import jinja2
import textwrap
import itertools

import numpy as np
from scipy import stats

# global requirements:
# - keep journal
# - keep original files for later processing
# - lightweight, "entities should not be multiplied beyond necessity"

# ======================================================================
# low-level read(), write(), mkdir(), subprocess.run() wrappers
# ======================================================================


def save_file(dirname, filename, data):
    """save data to specified dir/file"""
    try:
        path = os.path.join(dirname, filename)
        logging.debug("save data to {:s}".format(path))
        with open(path, "w") as f:
            f.write(data)
    except Exception as e:
        logging.error("Unable to save data to {:s}: {}".format(path, e))
        sys.exit(1)


def read_file(dirname, filename):
    """read specified dir/file and return string with file contents"""
    try:
        path = os.path.join(dirname, filename)
        logging.debug("reading file {:s}".format(path))
        with open(path, "r") as f:
            return f.read().rstrip()
    except Exception as e:
        logging.error("Error reading {:s}: {}".format(path, e))
        sys.exit(1)


def create_dir(dirname):
    """create directory with all intermediate-level directories needed to contain the leaf directory if necessary"""
    try:
        if not os.path.exists(dirname):
            logging.debug("create directory {:s}".format(dirname))
            os.makedirs(dirname)
    except OSError as e:
        logging.error("Unable to create directory: {}".format(e))
        sys.exit(1)


def run(*args, timeout=None):
    """run subprocess with given args and optional timeout"""
    try:
        logging.debug("run subprocess: {:s}".format(" ".join(args)))
        return subprocess.run(
            args,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except Exception as e:
        logging.error("Unable to run subprocess: {}".format(e))
        sys.exit(1)


# ======================================================================
# utility functions
# ======================================================================


def list_to_string(l):
    """convert list of strings, ints or floats to single comma-delimited string"""
    # join(l) will raise "TypeError: sequence item 0: expected str instance, int found" for ints
    return ", ".join([str(x) for x in l])


# ======================================================================
# generic logging functions
# ======================================================================

# log directory is more or less constant
# log file changes more often (stdout.log, stderr.log, data.json, output.json etc)
# keep dir name and file name in separate variables seems to be more convenient


def save_stdout_stderr(dirname, result):
    """save stdout and stderr (if exists) of completed process"""
    create_dir(dirname)
    if result.stdout:
        save_file(dirname, "stdout.log", result.stdout.decode("utf-8"))
    if result.stderr:
        save_file(dirname, "stderr.log", result.stderr.decode("utf-8"))


def save_dict(dirname, data):
    """save dict in JSON"""
    create_dir(dirname)
    save_file(dirname, "data.json", json.dumps(data, indent=4))


# ======================================================================
# sysfs reading functions
# ======================================================================


def read_dir_files(dirname, names):
    """read given files in a directory, return dict with name-contents pairs"""
    logging.debug("reading files: {:s}".format(list_to_string(names)))
    return {n: read_file(dirname, n) for n in names}


def read_dir(dirname):
    """read all files in a directory, return dict with name-contents pairs"""
    try:
        logging.debug("scanning directory {:s}".format(dirname))
        with os.scandir(dirname) as d:
            return read_dir_files(dirname, [e.name for e in d])
    except Exception as e:
        logging.error("Error reading {:s}: {}".format(dirname, e))
        sys.exit(1)


def get_queue_info(dev):
    """get /sys/block/<dev>/queue contents (scheduler and various parameters)"""
    # basename() to strip leading /dev/
    return read_dir(os.path.join("/sys/block", os.path.basename(dev), "queue"))


def get_nvme_module_info():
    """get nvme module version and parameters"""
    t = read_dir_files("/sys/module/nvme", ("version", "srcversion"))
    t["parameters"] = read_dir("/sys/module/nvme/parameters")
    return t


# ======================================================================
# external utilities (lshw, lspci, nvme) wrappers
# ======================================================================


def run_lshw():
    """get information on the hardware configuration"""
    return run("/usr/bin/sudo", "/usr/sbin/lshw", "-json", "-quiet", "-sanitize")


def run_lspci():
    """get PCI configuration"""
    return run("/usr/bin/sudo", "/sbin/lspci", "-vv")


def run_nvme_list():
    """get list of NVMe devices"""
    return run(
        "/usr/bin/sudo", "/usr/sbin/nvme", "list", "--output-format=json", "--verbose"
    )


def run_nvme_id_ctrl(dev):
    """get NVMe controller info"""
    return run(
        "/usr/bin/sudo", "/usr/sbin/nvme", "id-ctrl", "--output-format=json", dev
    )


def run_nvme_smart_log(dev):
    """get NVMe SMART info"""
    return run(
        "/usr/bin/sudo", "/usr/sbin/nvme", "smart-log", "--output-format=json", dev
    )


def run_nvme_get_feature(dev, fid):
    """get NVMe feature value for given feature id (fid)"""
    return run(
        "/usr/bin/sudo",
        "/usr/sbin/nvme",
        "get-feature",
        "--human-readable",
        "--feature-id=0x{:02x}".format(fid),
        dev,
    )


def run_nvme_set_feature(dev, fid, val):
    """set NVMe feature value for given feature id (fid)"""
    return run(
        "/usr/bin/sudo",
        "/usr/sbin/nvme",
        "set-feature",
        "--feature-id=0x{:02x}".format(fid),
        "--value=0x{:x}".format(val),
        dev,
    )


def run_nvme_format(dev):
    """pugre device using nvme format"""
    return run("/usr/bin/sudo", "/usr/sbin/nvme", "format", "-f", "-l0", dev)


# ======================================================================
# nvme get-feature parser
# ======================================================================

# https://nvmexpress.org/developers/nvme-specification/
# https://nvmexpress.org/wp-content/uploads/NVMe-NVM-Express-2.0a-2021.07.26-Ratified.pdf
# 5.27

#
# get-feature:0x7 (Number of Queues), Current value:0x1f001f
#        Number of IO Completion Queues Allocated (NCQA): 32
#        Number of IO Submission Queues Allocated (NSQA): 32
#


def get_nvme_features(dev):
    """return dict of selected NVMe features for a given device"""

    #
    # need only 0x07 Number of Queues maybe
    #
    FIDS = [0x01, 0x02, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A]

    #
    # first line is a kind of header with feature id, name and raw value:
    # get-feature:0x7 (Number of Queues), Current value:0x1f001f
    #
    p1 = re.compile(
        r"""get-feature:
            (?P<fid>\S+) \s+    # feature id
            \(
                (?P<name>[^)]+) # feature name
            \), \s+
            Current \s+ value:
            (?P<value>\S+)      # and feature value""",
        re.X,
    )

    #
    # list of "name (short name): value":
    #        Number of IO Completion Queues Allocated (NCQA): 32
    #
    p2 = re.compile(
        r"""\s*                  # leading spaces if any
            (?P<name>[^(]+) \s+  # anything until opening parentheses
            \(
                (?P<short>[^)]+) # short name in parentheses
            \) \s* : \s*
            (?P<value>\S+)       # and finally a value""",
        re.X,
    )

    logging.debug(
        "get features: device {:s}, FIDs {:s}".format(
            dev, list_to_string(["0x{:02x}".format(i) for i in FIDS])
        )
    )
    result = list()
    for fid in FIDS:

        r = run_nvme_get_feature(dev, fid)
        lines = r.stdout.decode("utf8").splitlines()

        # process the first line (feature name and raw value)
        m = p1.match(lines[0])
        if m:
            f = m.groupdict()
        else:
            logging.error("Unable to match: {:s}".format(lines[0]))
            sys.exit(1)

        # process the rest (name-value pairs)
        f["human-readable"] = list()
        for l in lines[1:]:
            m = p2.match(l)
            if m:
                f["human-readable"].append(m.groupdict())
            else:
                logging.error("Unable to match: {:s}".format(l))
                sys.exit(1)

        result.append(f)

    # print(json.dumps(result, indent=4))
    return result


# ======================================================================
# nvme get-list processing
# ======================================================================


def get_nvme_namespace(data, dev):
    """return namespace entry matching dev from nvme get-list output"""

    try:
        j = json.loads(data)
    except Exception as e:
        logging.error("JSON loading error: {}".format(e))
        sys.exit(1)

    # strip /dev/ from device path
    ns = os.path.basename(dev)

    # loop over Devices[] -> Controllers[] -> Namespaces[]
    logging.debug("Looking for {:s} namespace".format(ns))
    for d in j["Devices"]:
        for c in d["Controllers"]:
            for n in c["Namespaces"]:
                if n["NameSpace"] == ns:
                    return n

    logging.error("Namespace {:s} not found".format(ns))
    sys.exit(1)


# ======================================================================
# fio wrapper
# ======================================================================


def run_and_save_fio(dirname, job):
    """
    create job file, run fio and save result
    return result as a dict
    """

    # create log directory
    create_dir(dirname)

    # create job.fio file
    save_file(dirname, "job.fio", job)

    # run fio and save results to output.json file
    result = run(
        "/usr/bin/sudo",
        "/usr/bin/fio",
        "--output-format=json+",
        "--output={:s}".format(os.path.join(dirname, "output.json")),
        os.path.join(dirname, "job.fio"),
    )

    # save stdout and stderr (if present)
    save_stdout_stderr(dirname, result)

    # return result
    return json.loads(read_file(dirname, "output.json"))


# ======================================================================
# jinja2 template engine wrapper and templates
# ======================================================================

#
# TODO replace kwargs with single params dict ?
# to avoid ugly "queue_depth=queue_depth, thread_count=thread_count, seed=seed ..." lists
# and keep all parameters in single record (dataclass)
#


def render(template, **kwargs):
    """render template with provided args"""
    try:
        l = [f"{k}={v}" for k, v in kwargs.items()]
        logging.debug("Render template with args: {:s}".format(list_to_string(l)))
        e = jinja2.Environment(
            undefined=jinja2.StrictUndefined, autoescape=jinja2.select_autoescape()
        )
        t = e.from_string(textwrap.dedent(template))
        return t.render(kwargs)
    except Exception as e:
        logging.error("Rendering error: {}".format(e))
        sys.exit(1)


WIPC_JOB_TPL = """
    [global]
    bs=128k
    ioengine=libaio
    iodepth={{ queue_depth }}
    direct=1
    gtod_cpu=1
    thread
    group_reporting

    random_distribution=random
    random_generator=tausworthe
    allrandrepeat=1
    randseed={{ seed }}

    [wipc]
    stonewall
    filename={{ device }}
    rw=write
    numjobs={{ thread_count }}
    size={{ size }}
    io_size={{ io_size }}

"""

WDPC_IOPS_JOB_TPL = """
    [global]
    ioengine=libaio
    iodepth={{ queue_depth }}
    direct=1
    gtod_cpu=1
    thread
    group_reporting

    random_distribution=random
    random_generator=tausworthe
    allrandrepeat=1
    randseed={{ seed }}

    filename={{ device }}
    numjobs={{ thread_count }}
    size={{ size }}
    rw=randrw

    [wdpc-rr{{ read_rate }}-{{ block_size }}]
    stonewall
    runtime={{ runtime }}
    time_based
    rwmixread={{ read_rate }}
    bs={{ block_size }}

"""

# ======================================================================
# steady state
# SSS_PTS_2.0.2.pdf page 17
#
# 2.1.24 Steady State: A device is said to be in Steady State when, for the dependent variable (y) being tracked:
# a) Range(y) is less than 20% of Ave(y):
#    Max(y)-Min(y) within the Measurement Window is no more than 20% of the Ave(y) within the Measurement Window; and
# b) Slope(y) is less than 10%:
#    Max(y)-Min(y), where Max(y) and Min(y) are the maximum and minimum values on the best linear curve fit
#    of the y-values within the Measurement Window, is within 10% of Ave(y) value within the Measurement Window.
# ======================================================================


def in_steady_state(values, window_size):
    """return True if in steady state as defined in 2.1.24, False otherwise"""

    # must be at least window_size values in list
    if len(values) < window_size:
        logging.info("list too short, len {:d} < {:d}".format(len(values), window_size))
        return False

    # last window_size values
    yvalues = values[-window_size:]

    # calculate avg and range
    yavg = np.mean(yvalues)
    yrange = max(yvalues) - min(yvalues)
    logging.info("yavg = {:f}, yrange = {:f}".format(yavg, yrange))

    # Max(y)-Min(y) within the Measurement Window is no more than 20% of the Ave(y) within the Measurement Window
    if yrange >= 0.2 * yavg:
        logging.info(
            "a) failed, {:f} >= 0.2 * {:f} ({:f})".format(yrange, yavg, 0.2 * yavg)
        )
        return False
    logging.info("a) passed, {:f} < 0.2 * {:f} ({:f})".format(yrange, yavg, 0.2 * yavg))

    # x = number of round
    xvalues = list(range(len(values) - window_size + 1, len(values) + 1))

    # best linear fit
    res = stats.linregress(xvalues, yvalues)

    # calculate range for linear fit
    yrange_fit = abs(res.slope) * (window_size - 1)
    logging.info("yrange_fit = {:f}".format(yrange_fit))

    # Max(y)-Min(y), where Max(y) and Min(y) are the maximum and minimum values
    # on the best linear curve fit of the y-values within the Measurement Window
    # is within 10% of Ave(y) value within the Measurement Window.
    if yrange_fit >= 0.1 * yavg:
        logging.info(
            "b) failed, {:f} >= 0.1 * {:f} ({:f})".format(yrange_fit, yavg, 0.1 * yavg)
        )
        return False
    logging.info(
        "b) passed, {:f} < 0.1 * {:f} ({:f})".format(yrange_fit, yavg, 0.1 * yavg)
    )

    logging.info("all checks passed, in steady state")
    logging.info("linear fit formula: {:.3f}*R+{:.3f}".format(res.slope, res.intercept))
    logging.info("linear fit correlation coefficient: {:.3f}".format(res.rvalue))
    return True


# ======================================================================
# IOPS test
# SSS_PTS_2.0.2.pdf page 26
# ======================================================================


def iops(args):
    """IOPS test"""

    MEASUREMENT_WINDOW = 5

    # record test conditions
    save_stdout_stderr(os.path.join(args.output, "platform/lshw"), run_lshw())
    save_stdout_stderr(os.path.join(args.output, "platform/lspci"), run_lspci())
    save_stdout_stderr(
        os.path.join(args.output, "device/nvme-id-ctrl"), run_nvme_id_ctrl(args.dev)
    )
    save_stdout_stderr(
        os.path.join(args.output, "device/nvme-smart-log"), run_nvme_smart_log(args.dev)
    )
    save_dict(
        os.path.join(args.output, "settings/nvme-module-config"), get_nvme_module_info()
    )
    save_dict(
        os.path.join(args.output, "settings/nvme-features"), get_nvme_features(args.dev)
    )
    save_dict(os.path.join(args.output, "settings/queue"), get_queue_info(args.dev))

    # use nvme list to get device PhysicalSize
    nvme_list_result = run_nvme_list()
    save_stdout_stderr(os.path.join(args.output, "device/nvme-list"), nvme_list_result)
    ns = get_nvme_namespace(nvme_list_result.stdout.decode("utf-8"), args.dev)
    # align size to kbytes for direct i/o
    physical_size = int(ns["PhysicalSize"] / 1024)

    # For PTS-E, WCD and AR=100.  For PTS-C, WCE and AR=75.

    # 1 Purge the device. (Note: ActiveRange (AR) and other Test Parameters are not
    # applicable to Purge step; any values can be used and none need to be
    # reported.)

    if not args.test:
        save_stdout_stderr(
            os.path.join(args.output, "purge/nvme-format"), run_nvme_format(args.dev)
        )
    else:
        logging.info("Test mode, skipped nvme format on {:s}".format(args.dev))

    # 2 Run Workload Independent Pre-conditioning
    # 2.1 Set and record test conditions:
    # 2.1.1 Device volatile write cache PTS-E WCD, PTS-C WCE.
    # 2.1.2 OIO/Thread (aka Queue Depth (QD)): Test Operator Choice
    #       (recommended PTS-E QD=32; PTS-C QD=16)
    # 2.1.3 Thread Count (TC): Test Operator Choice (recommended PTS-E TC=4;
    #       PTS-C TC=2)

    if args.mode == "PTS-E":
        logging.info("PTS-E (enterprise) mode")
        # write cache disabled for enterprise devices
        save_stdout_stderr(
            os.path.join(args.output, "pre-conditioning/nvme-set-feature"),
            run_nvme_set_feature(args.dev, 0x06, 0),
        )
        active_range = physical_size
        queue_depth = 32
        thread_count = 4
    elif args.mode == "PTS-C":
        logging.info("PTS-C (client) mode")
        # write cache enabled for client devices
        save_stdout_stderr(
            os.path.join(args.output, "pre-conditioning/nvme-set-feature"),
            run_nvme_set_feature(args.dev, 0x06, 1),
        )
        active_range = int(0.75 * physical_size)
        queue_depth = 16
        thread_count = 2
    else:
        logging.error("Unknown PTS mode: {}".format(args.mode))
        sys.exit(1)

    # for development purposes only
    if args.test:
        runtime = "10s"
        logging.info("Test mode, set runtime to {:s}".format(runtime))
    else:
        runtime = "1m"

    # 2.1.4 Data Pattern: Required = Random, Optional = Test Operator
    seed = 0xDEADBEEF

    # 2.2 Run SEQ Workload Independent Pre-conditioning - Write 2X User Capacity
    #     with 128KiB SEQ writes, writing to the entire ActiveRange without LBA
    #     restrictions.

    # each thread limited to AR # and writes (2X of User Capacity) / thread_count bytes,
    # therefore all threads are writing 2X of User Capacity
    # TODO unclear in spec, clarify
    size = "{:d}k".format(active_range)
    io_size = "{:d}k".format(int(2 * physical_size / thread_count))

    # workload-independent pre-conditioning
    job = render(
        WIPC_JOB_TPL,
        device=args.dev,
        queue_depth=queue_depth,
        thread_count=thread_count,
        seed=seed,
        size=size,
        io_size=io_size,
    )
    if args.test:
        logging.info("Test mode, skipped WIPC on {:s}".format(args.dev))
    else:
        logging.info("run WIPC on {:s}".format(args.dev))
        run_and_save_fio(os.path.join(args.output, "pre-conditioning/wipc"), job)

    # 3 Run Workload Dependent Pre-conditioning and Test stimulus. Set test
    #   parameters and record for later reporting
    # 3.1 Set and record test conditions:
    # 3.1.1 Device volatile write cache PTS-E WCD, PTS-C WCE.
    # 3.1.2 OIO/Thread: Same as in step 2.1 above.
    # 3.1.3 Thread Count: Same as in step 2.1 above.
    # 3.1.4 Data Pattern: Required= Random, Optional = Test Operator Choice.

    # each thread limited to AR
    # no io_size, run 1 minute
    size = "{:d}k".format(active_range)

    # initialize lists with Steady State Tracking Variables
    rr0_4k_iops = list()
    rr65_64k_iops = list()
    rr100_1024k_iops = list()

    # 3.2 Run the following test loop until Steady State (SS) is reached, or maximum of 25 Rounds:
    # 'round' is a keyword, use round_num instead
    for round_num in range(1, 26):

        # 3.2.1 For (R/W Mix % = 100/0, 95/5, 65/35, 50/50, 35/65, 5/95, 0/100)
        # 3.2.1.1 For (Block Size = 1024KiB, 128KiB, 64KiB, 32KiB, 16KiB, 8KiB, 4KiB, 0.5KiB)
        # 3.2.1.2 Execute RND IO, per (R/W Mix %, Block Size), for 1 minute

        # 7 * 8 * 1m = 56m each run
        for rr in 100, 95, 65, 50, 35, 5, 0:
            for bs in "1024k", "128k", "64k", "32k", "16k", "8k", "4k", "512b":

                # device temperature before run
                save_stdout_stderr(
                    os.path.join(
                        args.output,
                        "round-{:d}".format(round_num),
                        "rr-{:d}".format(rr),
                        "bs-{:s}".format(bs),
                        "smart-before",
                    ),
                    run_nvme_smart_log(args.dev),
                )

                # run fio with single job in a file
                job = render(
                    WDPC_IOPS_JOB_TPL,
                    device=args.dev,
                    queue_depth=queue_depth,
                    thread_count=thread_count,
                    seed=seed,
                    size=size,
                    read_rate=rr,
                    block_size=bs,
                    runtime=runtime,
                )
                fio_result = run_and_save_fio(
                    os.path.join(
                        args.output,
                        "round-{:d}".format(round_num),
                        "rr-{:d}".format(rr),
                        "bs-{:s}".format(bs),
                        "wdpc",
                    ),
                    job,
                )

                # device temperature after run
                save_stdout_stderr(
                    os.path.join(
                        args.output,
                        "round-{:d}".format(round_num),
                        "rr-{:d}".format(rr),
                        "bs-{:s}".format(bs),
                        "smart-after",
                    ),
                    run_nvme_smart_log(args.dev),
                )

                # single job per fio run, job data available as first element of fio_result['jobs'] list
                # mean iops = p1 * read iops + p2 * write iops, where p1=rr, p2=1-rr
                iops = (rr / 100.0) * fio_result["jobs"][0]["read"]["iops"]
                iops += (1 - rr / 100.0) * fio_result["jobs"][0]["write"]["iops"]
                logging.info(
                    "round={:d} rr={:d} bs={:s} iops={:f}".format(
                        round_num, rr, bs, iops
                    )
                )

                # 3.2.1.2.2 Use IOPS Steady State Tracking Variables
                #              (R/W Mix% = 0/100, Block Size = 4KiB,
                #               R/W Mix%=65:35, Block Size = 64KiB,
                #               R/W Mix%=100/0, Block Size=1024KiB)
                # to detect Steady State where all Steady State Tracking Variables
                # meet the Steady State requirement.

                if (rr == 0) and (bs == "4k"):
                    rr0_4k_iops.append(iops)
                elif (rr == 65) and (bs == "64k"):
                    rr65_64k_iops.append(iops)
                elif (rr == 100) and (bs == "1024k"):
                    rr100_1024k_iops.append(iops)

        # check steady state after the end of the round
        logging.info("round {:d} finished".format(round_num))
        logging.info("rr0_4k_iops: [{:s}]".format(list_to_string(rr0_4k_iops)))
        logging.info("rr65_64k_iops: [{:s}]".format(list_to_string(rr65_64k_iops)))
        logging.info(
            "rr100_1024k_iops: [{:s}]".format(list_to_string(rr100_1024k_iops))
        )
        if (
            in_steady_state(rr0_4k_iops, MEASUREMENT_WINDOW)
            and in_steady_state(rr65_64k_iops, MEASUREMENT_WINDOW)
            and in_steady_state(rr100_1024k_iops, MEASUREMENT_WINDOW)
        ):
            logging.info("steady state, breaking the loop")
            break

        # 3.2.1.2.3 If Steady State is not reached by Round x=25, then the
        # Test Operator may either continue running the test
        # until Steady State is reached, or may stop the test at
        # Round x. The Measurement Window is defined as Round x-4
        # to Round x.

    if round_num == 25:
        logging.info("steady state was not reached in 25 rounds")


# ======================================================================
# main
# ======================================================================


def main():
    logging.basicConfig(
        format="%(filename)s:%(lineno)d %(funcName)s(): %(message)s", level=logging.INFO
    )

    parser = argparse.ArgumentParser(
        description="Implementation of SNIA Solid State Storage (SSS) Performance Test Specification (PTS)"
    )

    parser.add_argument("dev", type=str, help="Device to test")
    parser.add_argument(
        "-t",
        "--test",
        action="store_true",
        help="For development purposes only: skip device purge, skip WIPC, set runtime to 10s",
    )
    parser.add_argument(
        "-m",
        "--mode",
        default="PTS-C",
        type=str,
        choices=["PTS-C", "PTS-E"],
        help="Test mode: PTS-C (Client) or PTS-E (Enterprise)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=".",
        type=str,
        help="Output directory",
    )
    args = parser.parse_args()
    create_dir(args.output)
    iops(args)


if __name__ == "__main__":
    main()
