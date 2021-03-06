import sys
import io
import sh # type: ignore
import tempfile

from kgtk.exceptions import KGTKException


def parser():
    return {
        'help': 'Concatenate any mixture of plain or gzip/bzip2/xz-compressed files'
    }

def add_arguments(parser):
    parser.add_argument('-o', '--out', default=None, dest='output',
                        help='output file to write to, otherwise output goes to stdout')
    parser.add_argument('--gz', '--gzip', action='store_true', dest='gz',
                        help='compress result with gzip')
    parser.add_argument('--bz2', '--bzip2', action='store_true', dest='bz2',
                        help='compress result with bzip2')
    parser.add_argument('--xz', action='store_true', dest='xz',
                        help='compress result with xz')
    parser.add_argument("inputs", metavar="INPUT", nargs="*", action="store",
                        help="input files to process, if empty or `-' read from stdin")


### general command utilities (some of these should make it into a more central location):

tmp_dir = '/tmp'  # this should be configurable

def make_temp_file(prefix='kgtk.'):
    return tempfile.mkstemp(dir=tmp_dir, prefix=prefix)[1]

def get_buf_sizes(output=None, _tty_out=True, _piped=False):
    """Determine stream buffer sizes to use.  Since sh has complex rules for this depending on
    what streams are used and flags are set, we simply try this here and see if it lets us do it.
    We want to make sure to use large output buffers whenever possible for speed.
    This should probably go into cli_entry.py.
    """
    in_bufsize = 2**16
    out_bufsize = in_bufsize
    try:
        sh.ls.bake(_out=output, _out_bufsize=out_bufsize, _tty_out=_tty_out, _piped=_piped)
    except:
        out_bufsize = None
    return in_bufsize, out_bufsize

def get_stream_header(stream, n=1, unit='line', preserve=False):
    """Utility to access header information from a stream (usually stdin) without losing
    the ability to pass the rest or (if `preserve' is True) all of `stream' to other consumers.
    Reads `n' `unit's from stream (or fewer if `stream' doesn't have that much content) and
    returns the result as a byte sequence.  If `preserve' the second return value is a `sh'
    command sequence that can be piped into another sh-command with the full stream content.
    """
    header = io.BytesIO()
    if unit == 'line':
        for i in range(n):
            line = stream.readline()
            if line is None:
                break
            header.write(line)
    else:
        header.write(stream.read(n))
    if preserve:
        # we need to pass in the header with a temporary file which will be deleted when `cat' terminates.
        # alternatively to this scheme, we could create some kind of concatenated stream class:
        temp = make_temp_file('kgtk-header.')
        with open(temp, 'wb') as out:
            out.write(header.getvalue())
        cleanup = lambda cmd, status, exit_code: sh.rm('-f', temp)
        in_bufsize, out_bufsize = get_buf_sizes(_tty_out=False, _piped=True)
        return header.getvalue(), [sh.cat.bake(temp, '-', _in=stream, _in_bufsize=in_bufsize, _piped=True, _done=cleanup)]
    else:
        return header.getvalue()

def run_sh_commands(commands):
    """Run a single or list of prebaked sh `commands', compose them with pipes when they
    are marked with piped=True or bg=True.  Return the last run command which can be used
    to access the final exit_code and other state.
    """
    if not hasattr(commands, "__iter__"):
        commands = [commands]
    piped_output = None
    last_cmd = None
    for cmd in commands:
        if piped_output is not None:
            piped_output = cmd(piped_output)
        else:
            piped_output = cmd()
        last_cmd = piped_output
        # TO DO: improve this with a more explicit directive in case we don't use piped or bg:
        if not (cmd._partial_call_args.get('piped', False) or cmd._partial_call_args.get('bg', False)):
            piped_output = None
    return last_cmd

def determine_file_type(file):
    """Determine if `file' is compressed and if so how, and return file and its associated type.
    `file' needs to be a file name or a stream containing enough information to determine its type.
    """
    if isinstance(file, str):
        file_type = sh.file(file, '--brief').stdout
    else:
        file_type = sh.file('-', '--brief', _in=file).stdout
    # tricky: we get a byte sequence here which we have to decode into a string:
    return file_type.split()[0].lower().decode()

compression_type_table = {
    # we keep these strings, so we don't require the commands to be available during loading:
    'gzip':  {'cat': 'zcat',  'compress': 'gzip'},
    'bzip2': {'cat': 'bzcat', 'compress': 'bzip2'},
    'xz':    {'cat': 'xzcat', 'compress': 'xz'},
    'text':  {'cat': 'cat'},
}

def get_cat_command(file_type):
    """Determine a `cat' command based on a `file_type' determined by `determine_file_type'.
    """
    catcmd = compression_type_table.get(file_type, {}).get('cat', 'cat')
    # now return the equivalent of sh.cat, etc:
    return getattr(sh, catcmd)

def get_compress_command(file_type):
    """Return compress command to run based on target `file_type'.
    """
    compress = compression_type_table.get(file_type, {}).get('compress')
    return compress and getattr(sh, compress) or None


### zconcat implementation:

def compress_switch_to_file_type(gz=False, bz2=False, xz=False):
    """Return compressed target file_type based on supplied switches.
    """
    return (gz and 'gzip') or (bz2 and 'bzip2') or (xz and 'xz') or 'text'

def build_command_1(input=None, output=None, gz=False, bz2=False, xz=False, _piped=False, _out_mode='wb'):
    """Build a zconcat sh command pipe for a single `input'.
    If `_piped' is True, configure the last command to ignore `output' and write to a pipe
    in which case this can be used to feed into the input of another command (e.g., sort).
    `_out_mode' controls whether an `output' file will be truncated or appened to.
    """
    input = input or '-'
    output = (not _piped and (output or sys.stdout.buffer)) or None
    outfile = None
    if isinstance(output, str):
        outfile = open(output, _out_mode)
        output = outfile
    compress = get_compress_command(compress_switch_to_file_type(gz, bz2, xz))
    in_bufsize, out_bufsize = get_buf_sizes(output, not compress, _piped)
    
    if input == '-':
        # process input piped in from stdin, possibly compressed in different ways:
        input = sys.stdin.buffer
        header, input = get_stream_header(input, 1024, 'byte', True)
        file_type = determine_file_type(io.BytesIO(header))
        catcmd = get_cat_command(file_type)
        cleanup = lambda cmd, status, exit_code: outfile and outfile.close()
        if compress is not None:
            return input + [
                catcmd.bake(_piped=True),
                compress.bake('-c', _out=output, _out_bufsize=out_bufsize, _tty_out=False, _done=cleanup, _piped=_piped)
            ]
        else:
            return input + [
                catcmd.bake(_out=output, _out_bufsize=out_bufsize, _done=cleanup, _piped=_piped)
            ]
    else:
        # process a regular named file, possibly compressed in different ways:
        file_type = determine_file_type(input)
        catcmd = get_cat_command(file_type)
        cleanup = lambda cmd, status, exit_code: outfile and outfile.close()
        if compress is not None:
            return [
                catcmd.bake(input, _piped=True),
                compress.bake('-c', _out=output, _out_bufsize=out_bufsize, _tty_out=False, _done=cleanup, _piped=_piped)
            ]
        else:
            return [
                catcmd.bake(input, _out=output, _out_bufsize=out_bufsize, _done=cleanup, _piped=_piped)
            ]

def build_command(inputs=[], output=None, gz=False, bz2=False, xz=False):
    """Build a zconcat sh command pipe for the provided `inputs' and switches.
    """
    if len(inputs) == 0:
        inputs.append('-')
    command = []
    out_mode='wb'
    for inp in inputs:
        command.extend(build_command_1(input=inp, output=output, gz=gz, bz2=bz2, xz=xz, _out_mode=out_mode))
        out_mode='ab'
    return command

def run(inputs=[], output=None, gz=False, bz2=False, xz=False):
    """Run zconcat according to the provided command-line arguments.
    """
    try:
        commands = build_command(inputs=inputs, output=output, gz=gz, bz2=bz2, xz=xz)
        #print(commands)
        return run_sh_commands(commands).exit_code
    except sh.SignalException_SIGPIPE:
        # cleanup in case we piped and terminated prematurely:
        sys.stdout.flush()
    except Exception as e:
        #import traceback
        #traceback.print_tb(sys.exc_info()[2], 10)
        raise KGTKException('INTERNAL ERROR: ' + str(e) + '\n')

"""
# Examples:

> echo hello | kgtk zconcat
hello

> cat <<EOF > /tmp/file1
line1
line2
EOF
> cat <<EOF > /tmp/file2
line3
line4
EOF

> echo hello | kgtk ticker -i / zconcat --gz -o /tmp/out.gz /tmp/file1 - /tmp/file2
> 
> bzip2 /tmp/file1
> echo hello-again | kgtk zconcat /tmp/out.gz - /tmp/file1.bz2 
line1
line2
hello
>2020-04-01 15:57:48.750904
line3
line4
hello-again
line1
line2

> cat /tmp/file1.bz2 | kgtk zconcat
line1
line2

> cat /tmp/out.gz | kgtk zconcat
line1
line2
hello
>2020-04-02 18:36:40.000507
line3
line4

> cat /tmp/out.gz | kgtk zconcat | head -4
line1
line2
hello
>2020-04-07 14:04:15.962612

# speed test on a large 2GB compressed file:
> time kgtk zconcat -o /tmp/nodes-v2.csv.gz --gz /data/kgtk/wikidata/run1/nodes-v2.csv.gz
284.356u 6.955s 4:00.00 121.3%	0+0k 0+4063472io 0pf+0w
# elapsed time is the same as doing it directly in the shell:
> date; zcat /data/kgtk/wikidata/run1/nodes-v2.csv.gz | gzip -c > /tmp/nodes-v2.csv.gz; date
Tue 07 Apr 2020 10:39:50 AM PDT
Tue 07 Apr 2020 10:43:49 AM PDT
"""
