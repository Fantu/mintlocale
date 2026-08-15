#!/usr/bin/python3
"""PackageKit backend, used where aptkit is not available.

aptkit is not packaged everywhere mintlocale is (it is missing from Debian,
where the RFP is bug #1081742), so this module talks to PackageKit instead:
same model as aptkit, with a system daemon running the transaction, PolicyKit
for authorisation and progress and cancellation over D-Bus.

Only the subset of the aptkit API that mintlocale uses is implemented, with
the same names and the same call semantics, so install_remove.py and im.py
work with either backend:

    SimpleAPTClient(parent_window)
        set_finished_callback(cb)   -> cb(transaction=None, exit_state=None)
        set_cancelled_callback(cb)  -> cb()
        set_error_callback(cb)      -> cb(code, details)
        install_packages(names)
        remove_packages(names)
        update_cache()

install_file(), purge_packages(), downgrade_packages() and commit_changes()
are not implemented because mintlocale never calls them.  Note that
PackageKit has no purge at all - no method, no transaction flag - so removals
keep the configuration files, unlike aptkit's purge_packages().
"""

import gettext
import locale

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('PackageKitGlib', '1.0')
gi.require_version('XApp', '1.0')
from gi.repository import Gtk, GLib, Gio, XApp
from gi.repository import PackageKitGlib as Pk

# i18n
APP = 'mintlocale'
LOCALE_DIR = "/usr/share/linuxmint/locale"
locale.bindtextdomain(APP, LOCALE_DIR)
gettext.bindtextdomain(APP, LOCALE_DIR)
gettext.textdomain(APP)
_ = gettext.gettext

# PkBitfield is a plain guint64 of (1 << PkFilterEnum) bits; pk_bitfield_from_enums()
# is variadic and therefore not introspectable.
FILTER_INSTALL = (1 << Pk.FilterEnum.ARCH) | (1 << Pk.FilterEnum.NOT_INSTALLED)
FILTER_REMOVE = (1 << Pk.FilterEnum.ARCH) | (1 << Pk.FilterEnum.INSTALLED)

# pk_client_error_from_error_code() turns a daemon error into a GError of the
# PkClient domain whose code is the PkErrorEnum shifted past PK_CLIENT_ERROR_LAST.
PK_CLIENT_ERROR_LAST = 0xff


def pk_error_enum(error):
    """The PkErrorEnum behind a GError, or None if it carries none."""
    if error.domain != GLib.quark_to_string(Pk.client_error_quark()):
        return None
    if error.code <= PK_CLIENT_ERROR_LAST:
        return None
    return error.code - PK_CLIENT_ERROR_LAST


class MintLocaleTask(Pk.Task):
    """PkTask answering the questions the base class would decline.

    Pk.Task declines every question by default, which aborts the transaction.
    """

    def do_simulate_question(self, request, results):
        # TODO: list the additional packages and let the user refuse, the way
        # aptkit's AptConfirmDialog does.
        self.user_accepted(request)

    def do_untrusted_question(self, request, results):
        # Unsigned packages: refuse, as apt does by default.
        self.user_declined(request)

    def do_eula_question(self, request, results):
        self.user_declined(request)

    def do_key_question(self, request, results):
        self.user_declined(request)

    def do_media_change_question(self, request, results):
        self.user_declined(request)


class ProgressDialog(Gtk.Dialog):
    """Stand-in for aptkit.gtk3widgets.AptProgressDialog."""

    def __init__(self, parent, cancellable):
        Gtk.Dialog.__init__(self, transient_for=parent, modal=True,
                            destroy_with_parent=True)
        self.cancellable = cancellable
        self.set_default_size(400, -1)
        self.set_deletable(False)

        self.status_label = Gtk.Label(halign=Gtk.Align.START)
        self.progress_bar = Gtk.ProgressBar(show_text=True)

        box = self.get_content_area()
        box.set_spacing(6)
        box.set_border_width(12)
        box.add(self.status_label)
        box.add(self.progress_bar)

        self.cancel_button = self.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
        self.connect("response", self.on_response)
        self.show_all()

    def on_response(self, dialog, response_id):
        if response_id == Gtk.ResponseType.CANCEL:
            self.cancel_button.set_sensitive(False)
            self.cancellable.cancel()

    def update(self, progress, ptype):
        if ptype == Pk.ProgressType.PERCENTAGE:
            percentage = progress.props.percentage
            if 0 <= percentage <= 100:
                self.progress_bar.set_fraction(percentage / 100.0)
        elif ptype == Pk.ProgressType.STATUS:
            self.status_label.set_text(
                Pk.status_enum_to_localised_text(progress.props.status))
        elif ptype == Pk.ProgressType.ALLOW_CANCEL:
            self.cancel_button.set_sensitive(progress.props.allow_cancel)


class SimpleAPTClient(object):

    def __init__(self, parent_window=None):
        self.parent_window = parent_window
        self.progress_callback = None
        self.finished_callback = None
        self.error_callback = None
        self.cancelled_callback = None
        self.task = MintLocaleTask()
        self.task.set_interactive(True)
        self.task.set_locale(locale.getlocale(locale.LC_MESSAGES)[0] or "C")
        self.cancellable = None
        self.dialog = None

    def set_progress_callback(self, progress_callback):
        self.progress_callback = progress_callback

    def set_finished_callback(self, finished_callback):
        self.finished_callback = finished_callback

    def set_error_callback(self, error_callback):
        self.error_callback = error_callback

    def set_cancelled_callback(self, cancelled_callback):
        self.cancelled_callback = cancelled_callback

    def update_cache(self):
        self.start(lambda *args: self.task.refresh_cache_async(False, *args))

    def install_packages(self, packages, use_apt_resolver=True):
        # use_apt_resolver is accepted for call compatibility only: PackageKit
        # resolves dependencies in the daemon, so pulling them in beforehand
        # with python-apt, the way aptkit does, would serve no purpose.
        self.resolve_then(packages, FILTER_INSTALL,
                          lambda ids, *args: self.task.install_packages_async(ids, *args))

    def remove_packages(self, packages):
        # allow_deps=True, autoremove=False matches aptkit's remove_packages().
        self.resolve_then(packages, FILTER_REMOVE,
                          lambda ids, *args: self.task.remove_packages_async(ids, True, False, *args))

    def start(self, start_call, on_success=None):
        self.cancellable = Gio.Cancellable()
        if self.progress_callback is None:
            self.dialog = ProgressDialog(self.parent_window, self.cancellable)
        # The progress user_data has to be a tuple of the extra arguments the
        # callback takes: PyGObject rejects None with a TypeError on every
        # progress event. The async ready callback takes a plain object.
        start_call(self.cancellable, self.on_progress, (),
                   on_success or self.on_ready, None)

    def resolve_then(self, packages, filters, start_call):
        # PackageKit works on package ids ("name;version;arch;repo"), so the
        # names have to be resolved before the real transaction starts.
        def on_resolved(task, result, user_data):
            try:
                results = task.generic_finish(result)
            except GLib.Error as error:
                self.finish_with_error(error)
                return
            ids = [pkg.get_id() for pkg in results.get_package_array()]
            if not ids:
                # Nothing to do: already in the wanted state, or unknown names.
                self.close_dialog()
                if self.finished_callback is not None:
                    self.finished_callback()
                return
            start_call(ids, self.cancellable, self.on_progress, (),
                       self.on_ready, None)

        self.start(lambda *args: self.task.resolve_async(filters, packages, *args),
                   on_success=on_resolved)

    def on_progress(self, progress, ptype):
        if self.dialog is not None:
            self.dialog.update(progress, ptype)
        if ptype != Pk.ProgressType.PERCENTAGE:
            return
        if self.progress_callback is not None:
            self.progress_callback(progress.props.percentage)
        if self.parent_window is not None:
            XApp.set_window_progress(self.parent_window, progress.props.percentage)

    def on_ready(self, task, result, user_data):
        try:
            results = task.generic_finish(result)
        except GLib.Error as error:
            self.finish_with_error(error)
            return
        self.close_dialog()
        error_code = results.get_error_code()
        if error_code is not None:
            self.report_error(error_code.props.code, error_code.props.details)
            return
        if self.finished_callback is not None:
            self.finished_callback()

    def finish_with_error(self, error):
        self.close_dialog()
        if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
            if self.cancelled_callback is not None:
                self.cancelled_callback()
            return
        self.report_error(pk_error_enum(error), error.message)

    def report_error(self, code, details):
        if code in (Pk.ErrorEnum.NOT_AUTHORIZED, Pk.ErrorEnum.TRANSACTION_CANCELLED):
            # Authentication dismissed: same treatment as aptkit's NotAuthorizedError.
            if self.cancelled_callback is not None:
                self.cancelled_callback()
            return
        if self.error_callback is not None:
            self.error_callback(code, details)
            return
        dialog = Gtk.MessageDialog(transient_for=self.parent_window, modal=True,
                                   message_type=Gtk.MessageType.ERROR,
                                   buttons=Gtk.ButtonsType.CLOSE,
                                   text=_("The package operation failed"))
        dialog.format_secondary_text(details)
        dialog.run()
        dialog.destroy()

    def close_dialog(self):
        if self.parent_window is not None:
            XApp.set_window_progress(self.parent_window, 0)
        if self.dialog is not None:
            self.dialog.destroy()
            self.dialog = None
