## ================================================================================
## window.py
##
## Copyright (C) 2018 Jonas Møller (no) <jonasmo441@gmail.com>
## All rights reserved.
##
## Redistribution and use in source and binary forms, with or without
## modification, are permitted provided that the following conditions are met:
## 
## 1. Redistributions of source code must retain the above copyright notice, this
##    list of conditions and the following disclaimer.
## 2. Redistributions in binary form must reproduce the above copyright notice,
##    this list of conditions and the following disclaimer in the documentation
##    and/or other materials provided with the distribution.
## 
## THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
## ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
## WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
## DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
## FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
## DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
## SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
## CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
## OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
## OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
## SOFTWARE.
## ================================================================================

import time
import threading
import os
import sys
import shutil
import json
import re
from subprocess import Popen, PIPE, STDOUT as STDOUT_REDIR
from pprint import PrettyPrinter
from queue import Queue

import gi
from gi.repository import Gtk
from gi.repository import Gdk
from gi.repository import GObject
gi.require_version('GtkSource', '4')
from gi.repository import GtkSource
# from gi_composites import GtkTemplate

pprint = PrettyPrinter(indent = 4).pprint

HAWCK_HOME = os.path.join(os.getenv("HOME"), ".local", "share", "hawck")

LOCATIONS = {
    "scripts": os.path.join(HAWCK_HOME, "scripts"),
    "scripts-enabled": os.path.join(HAWCK_HOME, "scripts-enabled"),
    "first_use": os.path.join(HAWCK_HOME, ".user_has_been_warned"),
    "hawck_bin": "/usr/share/hawck/bin"
}

SCRIPT_DEFAULT = """
require "init"

-- Sample mappings:
down => {
  ctrl  + alt + key "h" => say "Hello world"
  shift + alt + key "w" => app("firefox"):new_window("https://github.com/snyball/Hawck")
  ctrl  + alt + key "k" => function ()
    p = io.popen("fortune")
    say(p:read("*a"))()
    p:close()
  end
}
"""[1:]

MODIFIER_NAMES = set([
    "Alt",
    "Alt_L",
    "Alt_R",
    "Control",
    "Control_L",
    "Control_R",
    "Shift",
    "Shift_L",
    "Shift_R",
    "Shift",
    "Shift_L",
    "Shift_R",
    "AltGr",
])

class HawckInstallException(Exception):
    pass

class KeyCap(GObject.Object):
    __gtype_name__ = "KeyCap"

    def __init__(self, win):
        super().__init__()
        self.win = win

    @GObject.Signal(flags=GObject.SignalFlags.RUN_LAST,
                    arg_types=(object, object),
                    return_type=bool,
                    accumulator=GObject.signal_accumulator_true_handled)
    def onKeyCaptureDone(self, *_):
        print(f"Keycap done: {_}")

class LogRetriever:
    def __init__(self):
        self.logs = []
        self.max_logs = 5 
        self.last_log_text = ""
        self.last_monotonic_time = 0
        self.dismissed = set()

    def append(self, log):
        """
        Return 1 if any logs needed to be popped, 0 otherwise.
        """
        num_removed = 0
        if len(self.logs) >= self.max_logs:
            self.logs = self.logs[1:]
            num_removed = 1
        self.logs.append(log)
        return num_removed

    def mklog(log):
        lua_err_rx = re.compile(r"^(.+):(\d)+: (.*)")
        log["TYPE"], _, log["MESSAGE"] = log["MESSAGE"].partition(":")
        if log["TYPE"].upper() == "LUA":
            m = lua_err_rx.match(log["MESSAGE"])
            log["LUA_FILE"], log["LUA_LINE"], log["LUA_ERROR"] = m.groups()
            log["LUA_LINE"] = int(log["LUA_LINE"])
        items = list(log.items())
        for (k, v) in items:
            if type(v) == str and v.isdigit():
                log[k] = int(v)
        return log

    def update(self):
        """
        Update logs, return new logs as well as the number of logs that
        were removed.
        """
        p = Popen(
            [os.path.join(LOCATIONS["hawck_bin"],
                            "get-lua-errors.sh"),
            ], stdout=PIPE, stderr=STDOUT_REDIR)
        ret = p.wait()
        if ret != 0:
            return [], 0
        out = p.stdout.read().decode("utf-8")
        # if out == self.last_log_text:
        #     return [], 0
        self.last_log_text = out
        added = []
        objs = []
        for line in out.splitlines():
            obj = json.loads(line.strip())
            objs.append(LogRetriever.mklog(obj))
        objs = sorted(objs, key=lambda o: o["__MONOTONIC_TIMESTAMP"])
        truncated_objs = []
        last_log = None
        if self.logs:
            last_log = self.logs[-1]
        skips = 0
        for obj in objs:
            ## Skip same messages
            if last_log and obj["MESSAGE"] == last_log["MESSAGE"]:
                skips += 1
            else: ## New log
                if last_log:
                    last_log["DUP"] = last_log.get("DUP", 1) + skips
                    truncated_objs.append(last_log)
                skips = 0
                last_log = obj
        if last_log:
            last_log["DUP"] = last_log.get("DUP", 1) + skips
            truncated_objs.append(last_log)
        added = list(o for o in truncated_objs if o["__MONOTONIC_TIMESTAMP"] > self.last_monotonic_time)
        if added:
            self.last_monotonic_time = added[-1]["__MONOTONIC_TIMESTAMP"]
        num_removed = 0
        for obj in added:
            num_removed += self.append(obj)
        return added, num_removed

class TemplateManager:
    def __init__(self, dir_path):
        self.dir_path = dir_path
        self.templates = {}

    ## Get builder instance of template
    def get(self, name):
        src = self.templates[name]
        builder = Gtk.Builder()
        builder.add_from_string(src)
        root = builder.get_object("root")
        root.unparent()
        return root, builder

    def load(self, name):
        with open(os.path.join(self.dir_path, name)) as f:
            self.templates[name] = f.read()

class HawckMainWindow(Gtk.ApplicationWindow):
    __gtype_name__ = 'HawckMainWindow'

    def __init__(self, **kwargs):
        settings = Gtk.Settings.get_default()
        settings.set_property("gtk-application-prefer-dark-theme", True)
        super().__init__(**kwargs)
        self.internal_call = 0
        GObject.type_register(GtkSource.View)
        self.edit_pages = []
        self.scripts = {}
        self.init_template()
        self.builder = Gtk.Builder()
        self.builder.add_from_file('window.ui')
        self.window = self.builder.get_object('HawckMainWindow')
        script_dir = LOCATIONS["scripts"]
        for fname in os.listdir(script_dir):
            _, ext = os.path.splitext(fname)
            if ext == ".hwk":
                self.addEditPage(os.path.join(script_dir, fname))
        self.insert_key_handler_id = self.connect("onKeyCaptureDone", self.insertKeyHandler)
        self.handler_block(self.insert_key_handler_id)
        self.window.connect("destroy", lambda *_: sys.exit(0))
        self.window.present()
        self.builder.connect_signals(self)
        script_sw = self.builder.get_object("script_enabled_switch")
        self.script_switch_handler_id = script_sw.connect("state-set", self.setScriptEnabled)
        self.prepareEditForPage(0)
        self.checkHawckDRunning()

        ## Key capture stuff
        ## TODO: Separate the key capturing into its own class
        self.keycap_names = []
        self.keycap_codes = []
        self.keycap_done = False

        ## Check for first use, issue warning if the program has not been launched before.
        if not os.path.exists(LOCATIONS["first_use"]):
            warning = self.builder.get_object("hawck_first_use_warning")
            warning.run()
            warning.hide()
            with open(LOCATIONS["first_use"], "w") as f:
                f.write("The user has been warned about potential risks of using the software.\n")

        self.templates = TemplateManager(".")
        self.templates.load("error_log.ui")
        self.logs = LogRetriever()
        self.updateLogs()

        notebook = self.builder.get_object("edit_notebook")
        notebook.set_current_page(0)

    def updateLogs(self):
        added, removed = self.logs.update()
        loglist = self.builder.get_object("script_error_list")

        for log in added:
            ## Create new row and prepend it
            row, builder = self.templates.get("error_log.ui")
            buf = builder.get_object("error_script_buffer")
            buf.set_text(log["LUA_ERROR"])
            label = builder.get_object("error_script_name")
            label.set_text(os.path.basename(log["LUA_FILE"]))
            num_dup_label = builder.get_object("num_duplicates")
            num_dup_label.set_text(str(log.get("DUP", 1)))
            open_btn = builder.get_object("error_script_btn_open")
            def openScript(*_):
                edit_pg = self.builder.get_object("edit_script_box")
                stack = self.builder.get_object("main_stack")
                edit_notebook = self.builder.get_object("edit_notebook")
                sname = HawckMainWindow.getScriptName(log["LUA_FILE"])
                script = self.scripts[sname]
                pagenr = script["pagenr"]
                view = script["view"]
                buf = script["buffer"]
                edit_notebook.set_current_page(pagenr)
                stack.set_visible_child(edit_pg)
                text_iter = buf.get_start_iter()
                text_iter.set_line(log["LUA_LINE"])
                # mark = Gtk.TextMark()
                # buf.add_mark(mark, text_iter)
                view.scroll_to_iter(text_iter, 0, True, 0.0, 0.17)
                errbuf = self.builder.get_object("script_error_buffer")
                errbuf.set_text(f"{sname}:{log['LUA_LINE']}: {log['LUA_ERROR']}")
            open_btn.connect("clicked", openScript)
            loglist.prepend(row)
            loglist.show_all()
            row.show_all()

    def onClickUpdateLogs(self, *_):
        self.updateLogs()

    ## TODO: Write this
    def onToggleAutoUpdateLog(self, *_):
        pass

    def addEditPage(self, path: str):
        scrolled_window = Gtk.ScrolledWindow()
        src_view = GtkSource.View()
        src_view.set_show_line_numbers(True)
        src_view.set_highlight_current_line(True)
        src_view.set_auto_indent(True)
        src_view.set_monospace(True)
        src_view.set_vexpand(True)
        buf = src_view.get_buffer()
        src_lang_manage = GtkSource.LanguageManager()
        lua_lang = src_lang_manage.get_language("lua")
        scheme_manager = GtkSource.StyleSchemeManager()
        print(f"Schemes: {scheme_manager.get_scheme_ids()}")
        scheme = scheme_manager.get_scheme("oblivion")
        buf.set_language(lua_lang)
        buf.set_style_scheme(scheme)
        with open(path) as f:
            buf.set_text(f.read())
        name = os.path.basename(path)
        notebook = self.builder.get_object("edit_notebook")
        scrolled_window.add(src_view)
        notebook.append_page(scrolled_window, Gtk.Label(name))
        notebook.show_all()
        pagenr = len(self.edit_pages)
        notebook.set_current_page(pagenr)
        self.edit_pages.append(path)
        sname = HawckMainWindow.getScriptName(path)
        self.scripts[sname] = {
            "pagenr": pagenr,
            "buffer": buf,
            "view": src_view
        }

    def onImportScriptOK(self, *_):
        file_chooser = self.builder.get_object("import_script_file_button")
        name_entry = self.builder.get_object("import_script_name_entry")
        name = name_entry.get_text()
        dst_path = os.path.join(LOCATIONS["scripts"], name + ".hwk")
        src_path = file_chooser.get_filename()
        shutil.copy(src_path, dst_path)
        self.addEditPage(dst_path)

    def prepareEditForPage(self, pagenr: int):
        print(f"Now editing: {self.edit_pages[pagenr]}")
        switch_obj = self.builder.get_object("script_enabled_switch")
        name = HawckMainWindow.getScriptName(self.edit_pages[pagenr])
        enabled_path = os.path.join(LOCATIONS["scripts-enabled"], name + ".lua")
        is_enabled = os.path.exists(enabled_path)
        with switch_obj.handler_block(self.script_switch_handler_id):
            switch_obj.set_state(is_enabled)
            switch_obj.set_active(is_enabled)

    def onEditChangePage(self, _notebook: Gtk.Notebook, _obj, pagenr: int):
        self.prepareEditForPage(pagenr)
    onEditSelectPage = onEditChangePage
    onEditSwitchPage = onEditChangePage

    def getCurrentEditFile(self):
        notebook = self.builder.get_object("edit_notebook")
        return self.edit_pages[notebook.get_current_page()]

    def onNewScriptOK(self, *_):
        popover = self.builder.get_object("new_script_popover")
        popover.popdown()
        name_entry = self.builder.get_object("new_script_name")
        path = os.path.join(LOCATIONS["scripts"], name_entry.get_text() + ".hwk")
        with open(path, "w") as f:
            f.write(SCRIPT_DEFAULT)
        self.addEditPage(path)

    def getCurrentBuffer(self):
        notebook = self.builder.get_object("edit_notebook")
        view = notebook.get_nth_page(notebook.get_current_page()).get_child()
        return view.get_buffer()

    def onTest(self, *_):
        print("Test")

    def saveCurrentScript(self):
        path = self.getCurrentEditFile()
        buf = self.getCurrentBuffer()
        start_iter = buf.get_start_iter()
        end_iter = buf.get_end_iter()
        text = buf.get_text(start_iter, end_iter, True)
        with open(path, "w") as wf:
            wf.write(text)

    def installScript(self, path: str):
        self.saveCurrentScript()
        p = Popen(
            [
                os.path.join(LOCATIONS["hawck_bin"],
                             "install_hwk_script.sh"),
                path
            ], stdout=PIPE, stderr=STDOUT_REDIR)
        out = p.stdout.read()
        ret = p.wait()

        ## Handle error
        if ret != 0:
            lines = out.splitlines()
            _ = lines.pop()
            raise HawckInstallException("\n".join(l.decode("utf-8") for l in lines))

    @staticmethod
    def getScriptName(hwk_path):
        name, _ = os.path.splitext(os.path.basename(hwk_path))
        return name

    def getCurrentScriptName(self):
        hwk_path = self.getCurrentEditFile()
        name, _ = os.path.splitext(os.path.basename(hwk_path))
        return name

    def onPopdown(self, p):
        p.popdown()

    def useScript(self, *_):
        current_file = self.getCurrentEditFile()
        buf = self.builder.get_object("script_error_buffer")
        try:
            self.installScript(current_file)
        except HawckInstallException as e:
            ## TODO: Display the error properly
            ## TODO: Parse Lua errors to get the line number of the error, then highlight this
            ##       in the text editor margin.
            popover = self.builder.get_object("use_script_error")
            popover.popup()
            buf.set_text(str(e))
            return
        buf.set_text("OK")
        popover = self.builder.get_object("use_script_success")
        popover.popup()
        HawckMainWindow.enableScript(self.getCurrentScriptName())

    @staticmethod
    def enableScript(name: str):
        HawckMainWindow.disableScript(name)
        name += ".lua"
        os.link(os.path.join(LOCATIONS["scripts"], name),
                os.path.join(LOCATIONS["scripts-enabled"], name))

    @staticmethod
    def disableScript(name: str) -> None:
        try:
            os.unlink(os.path.join(LOCATIONS["scripts-enabled"], name + ".lua"))
        except Exception:
            pass

    def setScriptEnabled(self, switch_obj: Gtk.Switch, enabled: bool):
        hwk_path = self.getCurrentEditFile()
        name = self.getCurrentScriptName()

        if not enabled:
            return HawckMainWindow.disableScript(name)

        buf = self.builder.get_object("script_error_buffer")
        try:
            print("Installing script ...")
            self.installScript(hwk_path)
            print("Success!")
        except HawckInstallException as e:
            print("EXCEPTION")
            # self.internal_call += 2
            # with switch_obj.handler_block(self.script_switch_handler_id):
            popover = self.builder.get_object("use_script_error")
            popover.popup()
            buf.set_text(str(e))
            switch_obj.set_active(False)
            switch_obj.set_state(False)
            return True

        buf.set_text("OK")

        HawckMainWindow.enableScript(name)

        return None

    def deleteScript(self, *_):
        popover = self.builder.get_object("delete_script_popover")
        popover.popdown()
        path = self.getCurrentEditFile()
        notebook = self.builder.get_object("edit_notebook")
        page_num = notebook.get_current_page()
        notebook.remove_page(page_num)
        pg = self.edit_pages
        name = self.getCurrentScriptName()
        HawckMainWindow.disableScript(name)
        self.edit_pages = pg[:page_num] + pg[page_num+1:]
        os.remove(path)

    def onClickAboutBtn(self, *_):
        # self.window.show_about_dialog()
        about_dialog = self.builder.get_object("hawck_about_dialog")
        about_dialog.run()
        about_dialog.hide()

    def saveScript(self, *_):
        sav_dialog = Gtk.FileChooserDialog("Save as", self,
                                           Gtk.FileChooserAction.SAVE,
                                           (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                                            Gtk.STOCK_SAVE, Gtk.ResponseType.OK))
        sav_dialog.set_transient_for(self.window)
        sav_dialog.set_do_overwrite_confirmation(True)
        sav_dialog.set_modal(True)
        sav_dialog.run()
        path = sav_dialog.get_filename()
        if path:
            shutil.copy(self.getCurrentEditFile(), path)
        sav_dialog.destroy()

    def insertKeyHandler(self, window, names, codes):
        self.handler_block(self.insert_key_handler_id)
        print(f"Captured: {names}")
        buf = self.getCurrentBuffer()
        text = "down + "
        text += " + ".join(n.lower() for n in names[:-1])
        text += f" + key \"{names[-1].lower()}\" => "
        buf.insert_at_cursor(text)

    def onInsertKey(self, *_):
        self.handler_unblock(self.insert_key_handler_id)
        self.captureKey()

    def checkHawckDRunning(self):
        inputd_sw = self.builder.get_object("inputd_switch")
        macrod_sw = self.builder.get_object("macrod_switch")
        pgrep_loc = "/usr/bin/pgrep"

        ret = Popen([pgrep_loc, "hawck-inputd"]).wait()
        inputd_sw.set_state(not ret)
        inputd_sw.set_active(not ret)

        ret = Popen([pgrep_loc, "hawck-macrod"]).wait()
        macrod_sw.set_state(not ret)
        macrod_sw.set_active(not ret)

    def onPanicBtn(self, *_):
        p = Popen([os.path.join(LOCATIONS["hawck_bin"], "kill9_hawck.sh")])
        p.wait()
        self.checkHawckDRunning()

    def onKeyCaptureCancel(self, *_):
        win = self.builder.get_object("key_capture_window")
        win.hide()

    def onKeyCaptureOK(self, *_):
        win = self.builder.get_object("key_capture_window")
        win.hide()
        names = self.keycap_names
        codes = self.keycap_codes
        self.keycap_names = []
        self.keycap_codes = []
        self.emit("onKeyCaptureDone", names, codes)

    def onKeyCaptureKeyRelease(self, window, ev):
        if self.keycap_done:
            return

        oname = ev.string.strip()
        ev_name = oname or Gdk.keyval_name(ev.keyval)
        is_modifier = ev_name in MODIFIER_NAMES
        if not is_modifier and len(ev_name) == 1:
            ev_name = ev_name.upper()
        try:
            idx = self.keycap_names.index(ev_name)
            self.keycap_codes.remove(self.keycap_codes[idx])
            self.keycap_names.remove(ev_name)
        except ValueError:
            return
        self.setKeyCaptureLabel(self.keycap_names)

    def onKeyCaptureReset(self, *_):
        self.keycap_names = []
        self.keycap_codes = []
        self.keycap_done = False
        self.setKeyCaptureLabel([])

    def onKeyCaptureKeyPress(self, window, ev):
        if self.keycap_done:
            return

        oname = ev.string.strip()
        ev_name = oname or Gdk.keyval_name(ev.keyval)

        print(f"name: {ev_name}")
        print(f"ev.keycode: {ev.keyval}")
        print(f"ev.hardware_keycode: {ev.hardware_keycode}")
        print(f"ev.string.strip(): {oname}")
        print(f"Gdk.keyval_name(ev.keyval): {Gdk.keyval_name(ev.keyval)}")
        print(f"Gdk.keyval_to_unicode(ev.keyval): {Gdk.keyval_to_unicode(ev.keyval)}")
        print("")

        is_modifier = ev_name in MODIFIER_NAMES

        if not is_modifier and len(ev_name) == 1:
            ev_name = ev_name.upper()

        ## Repeat key
        if self.keycap_names and self.keycap_names[-1] == ev_name:
            return

        self.keycap_names.append(ev_name)
        self.keycap_codes.append(ev.hardware_keycode)
        self.setKeyCaptureLabel(self.keycap_names)

        ## Check if we received a terminal key:
        if not is_modifier:
            self.keycap_done = True

    def setKeyCaptureLabel(self, names):
        fmt = " - ".join(names)
        label = self.builder.get_object("key_capture_display")
        label.set_text(fmt)

    @GObject.Signal(flags=GObject.SignalFlags.RUN_LAST,
                    arg_types=(object, object),
                    return_type=bool,
                    accumulator=GObject.signal_accumulator_true_handled)
    def onKeyCaptureDone(self, *_):
        print(f"Keycap done: {_}")
        self.onKeyCaptureReset()

    def captureKey(self):
        win = self.builder.get_object("key_capture_window")
        # cap = KeyCap(win)
        # self.emit("onKeyCaptureDone", ["Ctrl", "a"], [0, 0])
        # self.builder.connect_signals(cap)
        win.show_all()
