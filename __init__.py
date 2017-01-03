#
# Binary Ninja interface for RetDec decompiler
#
# Ref:
# * https://retdec.com/api/docs/decompiler.html
# * https://api.binary.ninja/
#
# @_hugsy_
#

from binaryninja import *

import tempfile
import time
import os
import sys
import requests
import re
import struct
import string


def read_cstring(view, addr):
    """Read a C string from address."""
    s = ""
    while True:
        c = view.read(addr, 1)
        if c not in string.printable:
            break
        if c == "\n": c = "\\n"
        if c == "\t": c = "\\t"
        if c == "\v": c = "\\v"
        if c == "\f": c = "\\f"
        s+= c
        addr += 1
    return s


class RetDecDecompiler(BackgroundTaskThread):
    """Defines a new background class for handling decompilation via RetDec to avoid UI blocking."""

    DECOMPILE_FILE_MODE        = 1
    DECOMPILE_FUNCTION_MODE    = 2
    DECOMPILE_RANGE_MODE       = 3

    def __init__(self, view, mode, *args, **kwargs):
        BackgroundTaskThread.__init__(self, '', True)
        self.view = None
        self.arch = None
        self.session = None

        if view.arch.name.lower() == "x86_64":
            show_message_box("RetDec", "RetDec does not support x86_64 decompilation yet...", OKButtonSet, InformationIcon)
            return

        self.arch = view.arch.name.lower()
        if self.arch.startswith("arm"):
            self.arch = "arm"
        elif self.arch.startswith("x86"):
            self.arch = "x86"
        elif self.arch.startswith("powerpc"):
            self.arch = "powerpc"
        elif self.arch.startswith("mips"):
            self.arch = "mips"
        else:
            log_error("Unsupported architecture: {}".format(self.arch))
            return

        self.api_key = self.get_or_create_key()
        if self.api_key is None:
            return

        self.session = self.new_retdec_session()
        self.title = ""
        self.mode = mode
        self.view = view

        if self.mode == RetDecDecompiler.DECOMPILE_FILE_MODE:
            self.title = "Decompiled '{}'".format(self.view.file.filename)
            progress_title = "Decompiling binary '{}' with RetDec...".format(self.view.file.filename)

        if self.mode == RetDecDecompiler.DECOMPILE_FUNCTION_MODE:
            func = args[0]
            self.title = "Decompiled '{}'".format(func.name)
            self.view.set_default_session_data("function", func)
            progress_title = "Decompiling function '{}' with RetDec...".format(func.name)

        if self.mode == RetDecDecompiler.DECOMPILE_RANGE_MODE:
            address = args[0]
            length = args[1]
            self.title = "Decompiled range {:#x}-{:#x}".format(address, address+length)
            self.view.set_default_session_data("address", address)
            self.view.set_default_session_data("length", length)
            progress_title = "Decompiling byte range with RetDec..."

        self.progress = progress_title
        return


    def get_or_create_key(self):
        """Retrieves a priorly entered API key for RetDec. If none existing, prompt for one and store
        it locally."""
        current_dir = os.path.dirname( os.path.realpath(__file__) )
        key_file = os.path.join(current_dir, "api_key")
        if not os.access(key_file, os.R_OK):
            log_warn("No API key, prompting for one...")
            key = get_text_line_input("Please enter your RetDec API key (https://retdec.com/account/):", "RetDec API")
            key = str(key.strip())
            if len(key)==0:
                log_error("No key provided")
                return None

            with open(key_file, 'w') as f:
                f.write(key+'\n')

            log_info("API key has been saved to disk")
        else:
            with open(key_file, 'r') as f:
                key = f.read().splitlines()[0]

        return key


    def new_retdec_session(self):
        """Creates a session for requests. """
        session = requests.Session()
        session.auth = (self.api_key, '')
        return session


    def submit_request(self, method, url, params={}):
        """Submit the current request."""
        log_debug("Sending {} request to '{}' with params: {}".format(method, url, params))

        method = getattr(self.session, method.lower())
        http = method(url, **params)
        if not (200 <= http.status_code < 300):
            log_error("Unexpected HTTP response code: received {}: {}".format(http.status_code, http.reason))
            log_debug(http.text)
            return False

        if http.headers.get("content-type") == "application/json":
            return http.json()
        return http.text


    def wait_until_finished(self, status_url, wait_time=1, max_tries=60):
        """Waits until the decompilation is finished."""
        while max_tries:
            response = self.submit_request('GET', status_url)
            if response['finished']:
                return response['succeeded']
            time.sleep(wait_time)
            max_tries -= 1
            if self.cancelled:
                return None

        return None


    def download_decompiled_code(self, outputs_url, _type="hll"):
        """Download the file generated by RetDec."""
        log_debug("Downloading output list from '{}'".format(outputs_url))
        http = self.submit_request("GET", outputs_url)
        if http == False:
            return "Error downloading from '{}'".format(outputs_url)

        output_url = http["links"].get(_type)
        http = self.submit_request("GET", output_url)
        if http == False:
            return "Error downloading from '{}'".format(output_url)

        return http


    def start_decompilation(self, params):
        """Starts the decompilation task and wait for the result."""
        decompile_url = "https://retdec.com/service/api/decompiler/decompilations"
        res = self.submit_request("POST", decompile_url, params)
        if res == False:
            return False
        self.progress = "New task created as '{}', waiting for decompilation to finish... ".format(res["id"])

        r = self.wait_until_finished(res["links"]["status"])
        if r is None:
            return False

        self.progress = "Decompilation done, downloading output..."
        raw_code = self.download_decompiled_code(res["links"]["outputs"])
        self.progress = "Integrating Binary Ninja symbols..."
        pcode = self.merge_binaryninja_symbols(raw_code, params["data"]["raw_entry_point"])
        self.progress = "Decompilation done"
        show_plain_text_report(self.title, pcode)
        return True


    def setup_retdec_params(self):
        """Setup the default RetDec parameters."""
        params = {
            "data": {},
            "files": {}
        }
        params["data"]["target_language"] = "c"
        params["data"]["raw_endian"]= "big" if self.view.endianness else "little"
        params["data"]["decomp_var_names"]= "readable"
        params["data"]["decomp_emit_addresses"]= "no"
        params["data"]["generate_cg"]= "no"
        params["data"]["generate_cfg"]= "no"
        params["data"]["comp_compiler"]= "gcc"
        return params


    def decompile_range_bytes(self):
        """Invoke this function to decompile for a specific range of bytes."""
        addr = self.view.session_data.address
        length = self.view.session_data.length

        if not self.view.is_valid_offset(addr):
            log_error("invalid")
            return

        p = self.setup_retdec_params()
        p["data"]["mode"] = "raw"
        p["data"]["architecture"] = self.arch
        p["data"]["file_format"] = self.view.view_type.lower()
        p["data"]["raw_section_vma"] = "{:#x}".format(addr)
        p["data"]["raw_entry_point"] = "{:#x}".format(addr)

        data = self.view.read(addr, length)
        if len(data)==0:
            log_warn("No data to decompile")
            return

        fd, filename = tempfile.mkstemp()
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        log_debug("Memory {:#x}-{:#x} written in {:s}".format(addr, addr+length, filename))

        with open(filename, "rb") as f:
            p["files"] = {"input": (os.path.basename(filename), f)}
            self.start_decompilation(p)

        os.unlink(filename)
        log_debug("Temporary file {:s} removed".format(filename))
        return


    def decompile_function(self):
        """Invoke this function to decompile for a specific function."""
        func = self.view.session_data.function
        start_addr = func.start
        end_addr   = max([x.end for x in func.basic_blocks])
        length = end_addr - start_addr

        self.view.set_default_session_data("address", start_addr)
        self.view.set_default_session_data("length", length)
        return self.decompile_range_bytes()


    def decompile_file(self):
        """Invoke this function to decompile the entire binary."""
        p = self.setup_retdec_params()
        p["data"]["mode"] = "bin"

        filepath = self.view.file.filename
        filename = os.path.basename(filepath)
        with open(filepath, "rb") as f:
            p["files"] = {"input": (filename, f)}
            self.start_decompilation(p)
        return


    def merge_binaryninja_symbols(self, code, entry_point_addr):
        """Use symbols defined in Binary Ninja to make the output more readable."""
        pcode = []
        patt = re.compile(r'(unknown_[a-f0-9]+|data_[a-f0-9]+|sub_[a-f0-9]+|0x[a-f0-9]+)')

        for line in code.splitlines():
            if line.strip().startswith('//') or line.strip().startswith('#'):
                pcode.append(line)
                continue

            log_debug("Analyzing line '{}'".format(line))
            if "entry_point" in line:
                addr = int(entry_point_addr, 16)
                sym = self.view.get_symbol_at(addr)
                if sym:
                    line = line.replace("entry_point", sym.name)

            for m in patt.findall(line):
                i = m.find('_')
                if i > 0:
                    addr = int(m.replace(m[:i+1], ''), 16)
                else:
                    addr = int(m, 16)
                log_debug("Trying to find symbol at {:#x}".format(addr))
                sym = self.view.get_symbol_at(addr)
                if sym:
                    line = line.replace(m, sym.name)
                else:
                    # try to deref, if fail, abort
                    new_addr = struct.unpack("<I", self.view.read(addr, 4))[0]
                    if self.view.is_offset_readable(new_addr):
                        log_debug("Trying to read string at {:#x}".format(new_addr))
                        cstring = read_cstring(self.view, new_addr)
                        if len(cstring):
                            line = line.replace(m, '"{:s}"'.format(cstring))

            pcode.append(line)

        pseudo_code = '\n'.join(pcode)
        return pseudo_code


    def run(self):
        if self.session is None:
            log_error("RetDec cannot run")
            return

        if self.mode == RetDecDecompiler.DECOMPILE_FILE_MODE:
            self.decompile_file()

        if self.mode == RetDecDecompiler.DECOMPILE_FUNCTION_MODE:
            self.decompile_function()

        if self.mode == RetDecDecompiler.DECOMPILE_RANGE_MODE:
            self.decompile_range_bytes()

        return


def function_decompile(view, function_name):
    bg_retdec = RetDecDecompiler(view, RetDecDecompiler.DECOMPILE_FUNCTION_MODE, function_name)
    bg_retdec.start()
    return


def bytes_decompile(self, addr, length):
    bg_retdec = RetDecDecompiler(view, RetDecDecompiler.DECOMPILE_RANGE_MODE, addr, length)
    bg_retdec.start()
    return


def file_decompile(view):
    bg_retdec = RetDecDecompiler(view, RetDecDecompiler.DECOMPILE_FILE_MODE)
    bg_retdec.start()
    return


PluginCommand.register_for_function("Decompile Function with RetDec",
                                    "Use RetDec decompiler to decompile the current function.",
                                    function_decompile)

# PluginCommand.register_for_range("Decompile selected range with RetDec",
#                                  "Use RetDec decompiler to decompile the selected bytes.",
#                                  bytes_decompile)

PluginCommand.register("Decompile Binary with RetDec",
                       "Use RetDec decompiler to decompile the binary.",
                       file_decompile)
