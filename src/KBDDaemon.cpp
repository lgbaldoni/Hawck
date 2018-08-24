/* =====================================================================================
 * Keyboard daemon.
 *
 * Copyright (C) 2018 Jonas Møller (no) <jonasmo441@gmail.com>
 * All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions are met:
 * 
 * 1. Redistributions of source code must retain the above copyright notice, this
 *    list of conditions and the following disclaimer.
 * 2. Redistributions in binary form must reproduce the above copyright notice,
 *    this list of conditions and the following disclaimer in the documentation
 *    and/or other materials provided with the distribution.
 * 
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
 * ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
 * WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
 * DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
 * FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
 * DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
 * SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
 * CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
 * OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
 * OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 * =====================================================================================
 */

#include <fstream>
#include <iostream>
#include <string>

extern "C" {
    #include <syslog.h>
    #include <grp.h>
}

#include "KBDDaemon.hpp"
#include "CSV.hpp"
#include "utils.hpp"
#include "Daemon.hpp"

// #undef DANGER_DANGER_LOG_KEYS
// #define DANGER_DANGER_LOG_KEYS 1

#if DANGER_DANGER_LOG_KEYS
    #warning "Currently logging keypresses"
    #warning "DANGER_DANGER_LOG_KEYS should **only** be enabled while debugging."
#endif

using namespace std;

constexpr int FSW_MAX_WAIT_PERMISSIONS_US = 5 * 1000000;

KBDDaemon::KBDDaemon() :
    kbd_com("/var/lib/hawck-input/kbd.sock")
{
    initPassthrough();
}

void KBDDaemon::addDevice(const std::string& device) {
    kbds.push_back(new Keyboard(device.c_str()));
}

KBDDaemon::~KBDDaemon() {
    for (Keyboard *kbd : kbds)
        delete kbd;
}

void KBDDaemon::unloadPassthrough(std::string path) {
    if (key_sources.find(path) != key_sources.end()) {
        auto vec = key_sources[path];
        for (int code : *vec)
            passthrough_keys.erase(code);
        delete vec;
        key_sources.erase(path);

        printf("RM: %s\n", path.c_str());

        // Re-add keys
        for (const auto &[_, vec] : key_sources)
            for (int code : *vec)
                passthrough_keys.insert(code);
    }
}

void KBDDaemon::loadPassthrough(std::string rel_path) {
    // FIXME: Actually handle these errors.
    try {
        // The CSV file is being reloaded after a change,
        // remove the old keys.
        char *rpath = realpath(rel_path.c_str(), nullptr);
        if (rpath == nullptr) {
            throw SystemError("Error in realpath() unable to get path for: " + rel_path);
        }
        string path(rpath);
        free(rpath);

        unloadPassthrough(path);

        CSV csv(path);
        auto cells = mkuniq(csv.getColCells("key_code"));
        auto cells_i = mkuniq(new vector<int>());
        for (auto *code_s : *cells) {
            int i;
            try {
                i = stoi(*code_s);
            } catch (const std::exception &e) {
                continue;
            }
            if (i >= 0) {
                passthrough_keys.insert(i);
                cells_i->push_back(i);
            }
        }
        key_sources[path] = cells_i.release();
        keys_fsw.add(path);
        printf("LOADED: %s\n", path.c_str());
    } catch (const CSV::CSVError &e) {
        cout << "loadPassthrough error: " << e.what() << endl;
    } catch (const SystemError &e) {
        cout << "loadPassthrough error: " << e.what() << endl;
    }
}

void KBDDaemon::loadPassthrough(FSEvent *ev) {
    unsigned perm = ev->stbuf.st_mode & (S_IRWXU | S_IRWXG | S_IRWXO);

    // Require that the file permission mode is 644 and that the file is
    // owned by the daemon user.
    if (perm == 0644 && ev->stbuf.st_uid == getuid()) {
        printf("OK: %s\n", ev->path.c_str());
        printf("LOG: perm=%X; uid=%d\n", perm, getuid());
        loadPassthrough(ev->path);
    } else {
        printf("ERROR: Invalid permissions for '%s', require 0644\n", ev->path.c_str());
        printf("LOG: perm=%X; uid=%d\n", perm, getuid());
    }
}

void KBDDaemon::initPassthrough() {
    auto files = mkuniq(keys_fsw.addFrom(data_dirs["keys"]));
    cout << "Added data_dir" << endl;
    for (auto &file : *files)
        loadPassthrough(&file);
}

void KBDDaemon::updateAvailableKBDs() {
    available_kbds.clear();
    for (auto &kbd : kbds)
        if (!kbd->isDisabled())
            available_kbds.push_back(kbd);
}

void KBDDaemon::run() {
    KBDAction action;

    for (auto& kbd : kbds) {
        cout << "Locking on to: " << kbd->getName() << endl;
        kbd->lock();
    }

    updateAvailableKBDs();

    // 30 consecutive socket errors will lead to the keyboard daemon
    // aborting.
    static const int MAX_ERRORS = 30;
    int errors = 0;

    keys_fsw.begin([&](FSEvent &ev) {
                       lock_guard<mutex> lock(passthrough_keys_mtx);
                       if (ev.mask & IN_DELETE_SELF)
                           unloadPassthrough(ev.path);
                       else if (ev.mask & (IN_CREATE | IN_MODIFY))
                           loadPassthrough(&ev);
                       return true;
                   });

    input_fsw.add("/dev/input/");
    input_fsw.setWatchDirs(true);
    input_fsw.setAutoAdd(false);

    struct group grpbuf;
    struct group *grp_ptr;
    char namebuf[200];

    // TODO: Write a getgroup function that either takes gid_t or string,
    //       it should return a struct Group by using either getgrnam_r or getgrgid_r.
    //       This function is ridiculous.
    //       Wrap grpbuf like this:
    //       struct Group {
    //           struct group grpbuf;
    //           char buf[200];
    //       }
    if (getgrnam_r("input", &grpbuf, namebuf, sizeof(namebuf), &grp_ptr) != 0)
        throw SystemError("Failure in getgrnam_r(): ", errno);
    gid_t input_gid = grp_ptr->gr_gid;

    #if 1
    input_fsw.begin([&](FSEvent &ev) {
                        // Don't react to the directory itself.
                        if (ev.path == "/dev/input")
                            return true;

                        cout << "Input event on: " << ev.path << endl;

                        lock_guard<mutex> lock(pulled_kbds_mtx);

                        for (auto it = pulled_kbds.begin(); it != pulled_kbds.end(); it++) {
                            auto kbd = *it;

                            int wait_inc_us = 100;
                            int wait_time = 0;
                            // Loop until the file has the correct permissions,
                            // when immediately added /dev/input/* files seem
                            // to be owned by root:root or by root:input with
                            // restrictive permissions.
                            // We expect it to be root:input with the input group
                            // being able to read and write.
                            struct stat stbuf;
                            unsigned grp_perm;
                            int ret;
                            do {
                                usleep(wait_inc_us);
                                ret = stat(ev.path.c_str(), &stbuf);
                                grp_perm = stbuf.st_mode & S_IRWXG;

                                // Check if it is a character device, test is done here because
                                // permissions might not allow for even stat()ing the file.
                                if (ret != -1 && !S_ISCHR(ev.stbuf.st_mode)) {
                                    // Not a character device, return
                                    cout << "Not a character device" << endl;
                                    return true;
                                }

                                if ((wait_time += wait_inc_us) > FSW_MAX_WAIT_PERMISSIONS_US) {
                                    syslog(LOG_ERR,
                                           "Could not aquire permissions rw with group input on '%s'",
                                           ev.path.c_str());
                                    // Skip this file
                                    return true;
                                }
                            } while (ret != -1 && !(4 & grp_perm && grp_perm & 2) && stbuf.st_gid != input_gid);

                            if (kbd->isMe(ev.path.c_str())) {
                                syslog(LOG_INFO,
                                       "Keyboard was plugged in: %s",
                                       kbd->getName().c_str());
                                kbd->reset(ev.path.c_str());
                                kbd->lock();
                                {
                                    lock_guard<mutex> lock(available_kbds_mtx);
                                    available_kbds.push_back(kbd);
                                }
                                pulled_kbds.erase(it);
                                break;
                            }
                        }
                        return true;
                    });
    #endif

    Keyboard *kbd = nullptr;
    bool had_key;
    for (;;) {
        had_key = false;
        action.done = 0;
        try {
            available_kbds_mtx.lock();
            vector<Keyboard*> kbds(available_kbds);
            available_kbds_mtx.unlock();
            int idx = kbdMultiplex(kbds, 64);
            if (idx != -1) {
                kbd = kbds[idx];
                kbd->get(&action.ev);

                // Throw away the key if the keyboard isn't locked yet.
                if (kbd->getState() == KBDState::LOCKED)
                    had_key = true;
            }
        } catch (const KeyboardError &e) {
            // Disable the keyboard,
            syslog(LOG_ERR,
                   "Read error on keyboard, assumed to be removed: %s",
                   kbd->getName().c_str());
            kbd->disable();
            {
                lock_guard<mutex> lock(available_kbds_mtx);
                auto pos_it = find(available_kbds.begin(),
                                   available_kbds.end(),
                                   kbd);
                available_kbds.erase(pos_it);
            }
            lock_guard<mutex> lock(pulled_kbds_mtx);
            pulled_kbds.push_back(kbd);
        }

        if (!had_key)
            continue;

        bool is_passthrough; {
            lock_guard<mutex> lock(passthrough_keys_mtx);
            is_passthrough = passthrough_keys.count(action.ev.code);
        }

        // Check if the key is listed in the passthrough set.
        if (is_passthrough) {
            // Pass key to Lua executor
            try {
                // TODO: Use select() and a time-out of about 1s here, in case the
                // macro daemon is taking way too long.

                kbd_com.send(&action);

                // Receive keys to emit from the macro daemon.
                for (;;) {
                    kbd_com.recv(&action);
                    if (action.done)
                        break;
                    udev.emit(&action.ev);
                }
                // Flush received keys and continue on.
                udev.flush();
                errors = 0;
                // Skip emmision of the key if everything went OK
                continue;
            } catch (SocketError &e) {
                cout << e.what() << endl;
                // If we're getting constant errors then the daemon needs
                // to be stopped, as the macro daemon might have crashed
                // or run into an error.
                if (errors++ > MAX_ERRORS) {
                    kbd_com.close();
                    syslog(LOG_CRIT, "Unable to communicate with MacroD");
                    abort();
                }
                // On error control flow continues on to udev.emit()
            }
        }

        udev.emit(&action.ev);
        udev.flush();
    }
}

