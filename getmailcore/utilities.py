#!/usr/bin/env python2.3
'''Utility classes and functions for getmail.
'''

__all__ = [
    'address_no_brackets', 'change_uidgid', 'deliver_maildir', 'eval_bool',
    'is_maildir', 'lock_file', 'logfile', 'mbox_from_escape', 'mbox_timestamp',
    'msg_flatten', 'msg_lines', 'unlock_file', 'updatefile'
]


import os
import signal
import time
import glob
import fcntl
import email
import cStringIO

# Only on Unix
try:
    import pwd
    import grp
except ImportError:
    pass

from exceptions import *

logtimeformat = '%Y-%m-%d %H:%M:%S'

#######################################
class updatefile(object):
    '''A class for atomically updating files.

    A new, temporary file is created when this class is instantiated.
    When the object's close() method is called, the file is synced to disk
    and atomically renamed to replace the original file.  close() is automatically
    called when the object is deleted.
    '''
    def __init__(self, filename):
        self.closed = False
        self.filename = filename
        self.tmpname = filename + '.tmp.%d' % os.getpid()
        try:
            f = open(self.tmpname, 'w')
        except IOError, (code, msg):
            raise IOError('%s, opening output file "%s"' % (msg, self.tmpname))
        self.file = f
        self.write = f.write
        self.flush = f.flush

    def __del__(self):
        self.close()

    def close(self):
        if self.closed:
            return
        self.file.flush()
        self.file.close()
        os.rename(self.tmpname, self.filename)
        self.closed = True

#######################################
class logfile(object):
    '''A class for locking and appending timestamped data lines to a log file.
    '''
    def __init__(self, filename):
        self.closed = False
        self.filename = filename
        try:
            self.file = open(os.path.expanduser(self.filename), 'a')
        except IOError, (code, msg):
            raise IOError('%s, opening file "%s"' % (msg, self.filename))

    def __del__(self):
        self.close()

    def __str__(self):
        return 'logfile(filename="%s")' % self.filename

    def close(self):
        if self.closed:
            return
        self.file.flush()
        self.file.close()
        self.closed = True

    def write(self, s):
        try:
            lock_file(self.file)
            # Seek to end
            self.file.seek(0, 2)
            self.file.write(time.strftime(logtimeformat, time.localtime()) + ' ' + s + os.linesep)
            self.file.flush()
        finally:
            unlock_file(self.file)

###################################
def alarm_handler(*unused):
    '''Handle an alarm during maildir delivery.

    Should never happen.
    '''
    raise getmailDeliveryError('Delivery timeout')

#######################################
def is_maildir(d):
    '''Verify a path is a maildir.
    '''
    for sub in ('', 'tmp', 'cur', 'new'):
        if not os.path.isdir(os.path.join(d, sub)):
            return False
    return True            

#######################################
def deliver_maildir(maildirpath, data, hostname, dcount=None):
    '''Reliably deliver a mail message into a Maildir.  Uses Dan Bernstein's
    documented rules for maildir delivery, and the updated naming convention
    for new files (modern delivery identifiers).  See
    http://cr.yp.to/proto/maildir.html and
    http://qmail.org/man/man5/maildir.html for details.
    '''
    if not is_maildir(maildirpath):
        raise getmailDeliveryError('not a Maildir (%s)' % maildirpath)

    # Set a 24-hour alarm for this delivery
    signal.signal(signal.SIGALRM, alarm_handler)
    signal.alarm(24 * 60 * 60)

    info = {
        'deliverycount' : dcount,
        'hostname' : hostname.split('.')[0].replace('/', '\\057').replace(':', '\\072'),
        'pid' : os.getpid(),
    }
    dir_tmp = os.path.join(maildirpath, 'tmp')
    dir_new = os.path.join(maildirpath, 'new')

    for unused in range(3):
        t = time.time()
        info['secs'] = int(t)
        info['usecs'] = int((t - int(t)) * 1000000)
        info['unique'] = 'M%(usecs)dP%(pid)s' % info
        if info['deliverycount'] is not None:
            info['unique'] += 'Q%(deliverycount)s' % info
        try:
            info['unique'] += 'R%s' % ''.join(['%02x' % ord(char) for char in open('/dev/urandom', 'rb').read(8)])
        except StandardError:
            pass

        filename = '%(secs)s.%(unique)s.%(hostname)s' % info
        fname_tmp = os.path.join(dir_tmp, filename)
        fname_new = os.path.join(dir_new, filename)

        # File must not already exist
        if os.path.exists(fname_tmp):
            # djb says sleep two seconds and try again
            time.sleep(2)
            continue

        # Be generous and check cur/file[:...] just in case some other, dumber
        # MDA is in use.  We wouldn't want them to clobber us and have the user
        # blame us for their bugs.
        curpat = os.path.join(maildirpath, 'cur', filename) + ':*'
        collision = glob.glob(curpat)
        if collision:
            # There is a message in maildir/cur/ which could be clobbered by
            # a dumb MUA, and which shouldn't be there.  Abort.
            raise getmailDeliveryError('collision with %s' % collision)

        # Found an unused filename
        break
    else:
        signal.alarm(0)
        raise getmailDeliveryError('failed to allocate file in maildir')

    # Get user & group of maildir
    s_maildir = os.stat(maildirpath)

    # Open file to write
    try:
        f = open(fname_tmp, 'wb')
        os.chmod(fname_tmp, 0600)
        try:
            # If root, change the message to be owned by the Maildir
            # owner
            os.chown(fname_tmp, s_maildir.st_uid, s_maildir.st_gid)
        except OSError:
            # Not running as root, can't chown file
            pass
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
        f.close()

    except IOError:
        signal.alarm(0)
        raise getmailDeliveryError('failure writing file ' + fname_tmp)

    # Move message file from Maildir/tmp to Maildir/new
    try:
        os.link(fname_tmp, fname_new)
        os.unlink(fname_tmp)

    except OSError:
        signal.alarm(0)
        try:
            os.unlink(fname_tmp)
        except:
            pass
        raise getmailDeliveryError('failure renaming "%s" to "%s"' % (fname_tmp, fname_new))

    # Delivery done

    # Cancel alarm
    signal.alarm(0)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)

    return filename

#######################################
def mbox_from_escape(s):
    '''Escape spaces, tabs, and newlines in the envelope sender address.'''
    return ''.join([(c in (' ', '\t', '\n')) and '-' or c for c in s])

#######################################
def mbox_timestamp(t=time.time(), localtime=False):
    '''Return the current time in the format expected in an mbox From_ line.'''
    return time.asctime(time.gmtime(time.time()))

#######################################
def lock_file(file):
    '''Do fcntl file locking.'''
    fcntl.flock(file.fileno(), fcntl.LOCK_EX)

#######################################
def unlock_file(file):
    '''Do fcntl file unlocking.'''
    fcntl.flock(file.fileno(), fcntl.LOCK_UN)

#######################################
def address_no_brackets(addr):
    '''Strip surrounding <> on an email address, if present.'''
    return (addr.startswith('<') and addr.endswith('>')) and addr[1:-1] or addr

#######################################
def eval_bool(s):
    '''Handle boolean values intelligently.
    '''
    val = str(s).lower()
    if val in ('0', 'false', 'no', 'off'):
        return False
    elif val in ('1', 'true', 'yes', 'on'):
        return True
    return bool(eval(val))

#######################################
def change_uidgid(logger, user=None, _group=None):
    '''Change the current effective GID and UID to those specified by user and _group.
    '''
    try:
        if _group:
            logger.debug('Getting GID for specified group %s\n' % _group)
            try:
                run_gid = grp.getgrnam(_group).gr_gid
            except KeyError, o:
                raise getmailConfigurationError('no such specified group (%s)' % o)
            if os.getegid() != run_gid:
                logger.debug('Setting egid to %d\n' % run_gid)
                os.setegid(run_gid)
        if user:
            logger.debug('Getting UID for specified user %s\n' % user)
            try:
                run_uid = pwd.getpwnam(user).pw_uid
            except KeyError, o:
                raise getmailConfigurationError('no such specified user (%s)' % o)
            if os.geteuid() != run_uid:
                logger.debug('Setting euid to %d\n' % run_uid)
                os.seteuid(run_uid)
    except OSError, o:
        raise getmailDeliveryError('change UID/GID to %s/%s failed (%s)' % (user, _group, o))

#######################################
def msg_lines(msg, mangle_from=False, include_from=False, max_linelen=998):
    '''Take a message object and return a list of text lines.
    '''
    f = cStringIO.StringIO()
    gen = email.Generator.Generator(f, mangle_from, max_linelen)
    gen.flatten(msg, include_from)
    # email module apparently doesn't always use native EOL, so force it
    f.seek(0)
    return f.read().splitlines()

#######################################
def msg_flatten(msg, mangle_from=False, include_from=False, max_linelen=998):
    '''Take a message object and return a string with native EOL convention.
    '''
    # email module apparently doesn't always use native EOL, so force it
    return os.linesep.join(msg_lines(msg, mangle_from, include_from, max_linelen))
