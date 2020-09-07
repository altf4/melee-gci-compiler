"""compiler.py: Compiles MGC files into a block of data that is ready to write
to the GCI."""
from pathlib import Path
import logger
from logger import log
from errors import *
from lineparser import *
from mgc_file import MGCFile, GeckoCodelistFile, BINFile
from gci_tools.mem2gci import *

# The earliest location we can inject data into the GCI
GCI_START_OFFSET = 0x2060

# The total size of a Melee GCI - this gets replaced by our input GCI file
gci_data = bytearray(0x16040)

# The latest !loc and !gci pointers
loc_pointer = 0
gci_pointer = 0
gci_pointer_mode = False

# A dict that contains all loaded MGC filedata, accessible by filename.
# We load all MGC files from disk ahead of time and use this dict to send
# file data to any MGCFile objects we create.
mgc_files = {}
bin_files = {}
geckocodelist_files = {}

# A list of the current MGC file stack if there are nested source files
mgc_stack = []

# A list of (address, length) tuples used to see if writing to the same address
# more than once
write_history = []

# The directory of the root MGC file
root_directory = ""

def compile(root_mgc_path, silent=False, noclean=False, debug=False):
    """Main compile routine: Takes a root MGC file path and compiles all data"""
    global write_history
    logger.silent_log = silent
    logger.debug_log = debug
    if not noclean:
        # Compile init_gci.mgc which writes the data found in a clean save file
        # TODO: Zero out all block data before this step
        log('INFO', "Initializing GCI")
        gci_data[GCI_START_OFFSET:] = bytearray(len(gci_data) - GCI_START_OFFSET)
        compile(Path(__file__).parent/"init_gci"/"init_gci.mgc", silent=True, noclean=True)
    logger.silent_log = silent
    logger.debug_log = debug
    write_history = []
    # Set root directory
    root_mgc_path = Path(root_mgc_path).absolute()
    root_directory = root_mgc_path.parent
    # Load all src files into mgc_files
    _load_all_mgc_files(root_mgc_path)
    # Begin compile
    _compile_file(mgc_files[root_mgc_path])
    # Temporary write routine for testing
    with open("temp.gci", 'wb') as f:
        f.write(gci_data)
    return

def _compile_file(mgc_file, ref_mgc_file=None, ref_line_number=None):
    """Compiles the data of a single file; !src makes this function recursive"""
    log('INFO', f"Compiling {mgc_file.filepath.name}", ref_mgc_file, ref_line_number)
    if mgc_file in mgc_stack:
        raise CompileError("MGC files are sourcing each other in an infinite loop", ref_mgc_file, ref_line_number)
    mgc_stack.append(mgc_file)
    for line in mgc_file.get_lines():
        for op in line.op_list:
            OPCODE_FUNCS[op.codetype](op.data, mgc_file, line.line_number)
    mgc_stack.pop()

def _load_mgc_file(filepath):
    """Loads a MGC file from disk and stores its data in mgc_files"""
    if filepath in mgc_files: return [] # Do nothing if the file is already loaded
    log('INFO', f"Loading MGC file {filepath.name}")
    filedata = []
    parent = filepath.parent
    try:
        with filepath.open('r') as f:
            filedata = f.readlines()
    except FileNotFoundError:
        raise CompileError(f"File not found: {str(filepath)}")
    except UnicodeDecodeError:
        raise CompileError("Unable to read MGC file; make sure it's a text file")
    # Store file data
    mgc_files[filepath] = MGCFile(filepath, filedata)
    # See if the new file sources any additional files we need to load
    # TODO: Handle binary files and geckocodelist files
    additional_files = []
    for line in filedata:
        op_list = parse_opcodes(line)
        for operation in op_list:
            if operation.codetype != 'COMMAND': continue
            if operation.data.name == 'src':
                additional_files.append(parent.joinpath(operation.data.args[0]))
            elif operation.data.name == 'geckocodelist':
                _load_geckocodelist_file(parent.joinpath(operation.data.args[0]))
            elif operation.data.name == 'file':
                _load_bin_file(parent.joinpath(operation.data.args[0]))
    return additional_files

def _load_geckocodelist_file(filepath):
    """Loads a Gecko codelist file from disk and stores its data in
       geckocodelist_files"""
    if filepath in geckocodelist_files: return
    log('INFO', f"Loading Gecko codelist file {filepath.name}")
    try:
        with filepath.open('r') as f:
            filedata = f.readlines()
    except FileNotFoundError:
        raise CompileError(f"File not found: {str(filepath)}")
    except UnicodeDecodeError:
        raise CompileError("Unable to read Gecko codelist file; make sure it's a text file")
    geckocodelist_files[filepath] = GeckoCodelistFile(filepath, filedata)
    return

def _load_bin_file(filepath):
    """Loads a binary file from disk and stores its data in bin_files"""
    if filepath in bin_files: return
    log('INFO', f"Loading binary file {filepath.name}")
    try:
        with filepath.open('rb') as f:
            filedata = f.read()
    except FileNotFoundError:
        raise CompileError(f"File not found: {str(filepath)}")
    bin_files[filepath] = BINFile(filepath, filedata)

def _load_all_mgc_files(root_filepath):
    """Loads all required MGC files from disk, starting with the root file"""
    additional_files = _load_mgc_file(root_filepath)
    # This shouldn't infinite loop because already-loaded files return None
    for path in additional_files:
        additional_files += _load_mgc_file(path)



def _write_data(data, mgc_file, line_number):
    """Writes a byte array to the GCI"""
    global gci_data, loc_pointer, gci_pointer, gci_pointer_mode, write_history
    if gci_pointer_mode:
        log('DEBUG', f"Writing 0x{len(data):x} bytes in gci mode:", mgc_file, line_number)
        if gci_pointer + len(data) > len(gci_data):
            raise CompileError("Attempting to write past the end of the GCI", mgc_file, line_number)
        write_table = [(gci_pointer, len(data))]
        gci_pointer += len(data)
    else:
        log('DEBUG', f"Writing 0x{len(data):x} bytes in loc mode:", mgc_file, line_number)
        try:
            write_table = data2gci(loc_pointer, len(data))
        except ValueError as e:
            raise CompileError(e, mgc_file, line_number)
        loc_pointer += len(data)
    data_pointer = 0
    _check_write_history(write_table, mgc_file, line_number)
    for entry in write_table:
        gci_pointer, data_length = entry
        log('DEBUG', f"        0x{data_length:x} bytes to 0x{gci_pointer:x}", mgc_file, line_number)
        gci_data[gci_pointer:gci_pointer+data_length] = data[data_pointer:data_pointer+data_length]
        data_pointer += data_length
    write_history.append((write_table, mgc_file, line_number))
    return

def _check_write_history(write_table, mgc_file, line_number):
    global write_history
    for entry in write_table:
        gci_pointer, data_length = entry
        for history_entry in write_history:
            history_write_table = history_entry[0]
            history_mgc_file = history_entry[1]
            history_line_number = history_entry[2]
            for history_write_table_entry in history_write_table:
                history_pointer, history_length = history_write_table_entry
                if (history_pointer <= gci_pointer and history_pointer + history_length > gci_pointer) or \
                   (gci_pointer <= history_pointer and gci_pointer + data_length > history_pointer):
                    log('WARNING', f"GCI location 0x{max(history_pointer, gci_pointer):x} was already written to by {history_mgc_file.filepath.name} (Line {history_line_number+1}) and is being overwritten", mgc_file, line_number)
                    return

def _process_bin(data, mgc_file, line_number):
    global gci_data, loc_pointer, gci_pointer, gci_pointer_mode
    data_hex = format(int(data, 2), 'x')
    _process_hex(data_hex, mgc_file, line_number)
    return
def _process_hex(data, mgc_file, line_number):
    global gci_data, loc_pointer, gci_pointer, gci_pointer_mode
    data = bytearray.fromhex(data)
    _write_data(data, mgc_file, line_number)
    return
def _process_command(data, mgc_file, line_number):
    global gci_data, loc_pointer, gci_pointer, gci_pointer_mode
    COMMAND_FUNCS[data.name](data.args, mgc_file, line_number)
    return
def _process_warning(data, mgc_file, line_number):
    global gci_data, loc_pointer, gci_pointer, gci_pointer_mode
    log('WARNING', data, mgc_file, line_number)
    return
def _process_error(data, mgc_file, line_number):
    global gci_data, loc_pointer, gci_pointer, gci_pointer_mode
    raise CompileError(data, mgc_file, line_number)
    return



def _cmd_process_loc(data, mgc_file, line_number):
    global gci_data, loc_pointer, gci_pointer, gci_pointer_mode
    gci_pointer_mode = False
    loc_pointer = data[0]
    return
def _cmd_process_gci(data, mgc_file, line_number):
    global gci_data, loc_pointer, gci_pointer, gci_pointer_mode
    gci_pointer_mode = True
    gci_pointer = data[0]
    return
def _cmd_process_add(data, mgc_file, line_number):
    global gci_data, loc_pointer, gci_pointer, gci_pointer_mode
    if gci_pointer_mode: gci_pointer += data[0]
    else: loc_pointer += data[0]
    return
def _cmd_process_src(data, mgc_file, line_number):
    global gci_data, loc_pointer, gci_pointer, gci_pointer_mode
    file = mgc_file.filepath.parent.joinpath(data[0])
    _compile_file(mgc_files[file], mgc_file, line_number)
    return
def _cmd_process_file(data, mgc_file, line_number):
    global gci_data, loc_pointer, gci_pointer, gci_pointer_mode
    file = mgc_file.filepath.parent.joinpath(data[0])
    _write_data(bin_files[file].filedata, mgc_file, line_number)
    return
def _cmd_process_geckocodelist(data, mgc_file, line_number):
    global gci_data, loc_pointer, gci_pointer, gci_pointer_mode
    file = mgc_file.filepath.parent.joinpath(data[0])
    _write_data(geckocodelist_files[file].filedata, mgc_file, line_number)
    return
def _cmd_process_string(data, mgc_file, line_number):
    global gci_data, loc_pointer, gci_pointer, gci_pointer_mode
    _write_data(bytearray(data[0], encoding='ascii'), mgc_file, line_number)
    return
def _cmd_process_asm(data, mgc_file, line_number):
    global gci_data, loc_pointer, gci_pointer, gci_pointer_mode
    asm_block_num = int(data[0])
    _process_hex(mgc_file.asm_blocks[asm_block_num], mgc_file, line_number)
    return
def _cmd_process_asmend(data, mgc_file, line_number):
    global gci_data, loc_pointer, gci_pointer, gci_pointer_mode
    log('WARNING', "!asmend is used without a !asm preceding it", mgc_file, line_number)
    return
def _cmd_process_c2(data, mgc_file, line_number):
    global gci_data, loc_pointer, gci_pointer, gci_pointer_mode
    asm_block_num = int(data[1])
    _process_hex(mgc_file.asm_blocks[asm_block_num], mgc_file, line_number)
    return
def _cmd_process_c2end(data, mgc_file, line_number):
    global gci_data, loc_pointer, gci_pointer, gci_pointer_mode
    log('WARNING', "!c2end is used without a !c2 preceding it", mgc_file, line_number)
    return
def _cmd_process_begin(data, mgc_file, line_number):
    global gci_data, loc_pointer, gci_pointer, gci_pointer_mode
    log('WARNING', "!begin is used more than once; ignoring this one", mgc_file, line_number)
    return
def _cmd_process_end(data, mgc_file, line_number):
    global gci_data, loc_pointer, gci_pointer, gci_pointer_mode
    log('WARNING', "!end is used more than once; ignoring this one", mgc_file, line_number)
    return
def _cmd_process_echo(data, mgc_file, line_number):
    global gci_data, loc_pointer, gci_pointer, gci_pointer_mode
    log('INFO', data[0])
    return



OPCODE_FUNCS = {
    'BIN': _process_bin,
    'HEX': _process_hex,
    'COMMAND': _process_command,
    'WARNING': _process_warning,
    'ERROR': _process_error,
}

COMMAND_FUNCS = {
    'loc': _cmd_process_loc,
    'gci': _cmd_process_gci,
    'add': _cmd_process_add,
    'src': _cmd_process_src,
    'file': _cmd_process_file,
    'geckocodelist': _cmd_process_geckocodelist,
    'string': _cmd_process_string,
    'asm': _cmd_process_asm,
    'asmend': _cmd_process_asmend,
    'c2': _cmd_process_c2,
    'c2end': _cmd_process_c2end,
    'begin': _cmd_process_begin,
    'end': _cmd_process_end,
    'echo': _cmd_process_echo,
}