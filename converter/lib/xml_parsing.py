##################################################
# File: xml_parsing.py
# Author: Chris Conover
# Description: Captures vairables, source/sink
#              blocks and type information
# Created: Wed July 5 13:30:00 2017
##################################################

import re
import sys
from collections import namedtuple
import itertools
import xml.etree.ElementTree as ET

RHBlock = namedtuple("RHBlock", "name type direction")

class GNUBlock(object):
    def __init__(self, block_type="", name="", value="", label="", type_="", refs=[]):
        self.block_type = block_type
        self.name = name
        self.value = value
        self.label = label
        self.type = type_
        self.refs = refs

    def __str__(self):
        return "{0}({1})".format(type(self).__name__, self.__dict__)

    # Returns a GNUBlock instance if the block is enabled.  Otherwise None.
    @staticmethod
    def from_xml(block):
        # Iterate over the block's params
        block_type = block.find("key").text.lower()
        name = value = label = type_ = ""
        refs = []
        for param in block.findall('param'):
            param_key = param.find('key').text.lower()
            param_value = param.find('value').text
            
            """
            2020-06-15
                Swap to support numeric booleans
                Tested with GNU Radio 3.7.14
            """
            #if param_key == "_enabled" and "true" != param_value.lower():
            #    # Skip disabled blocks
            #    # EARLY RETURN
            #    return None
            if param_key == "_enabled":
                if param_value.lower() == "true":
                    continue # enabled with true
                elif param_value.lower() in ["false", "none"]:
                    return None # disabled with false or None
                elif not bool(int(param_value)):
                    return None # disabled component, return None

            # Map other known keys to fields or treat any unknowns as possible
            # references to variable IDs (if the key starts with an alpha char)
            elif param_key == "id":
                name = param_value
            elif param_key == "value":
                value = param_value
            elif param_key == "label":
                label = param_value
            elif param_key == "type":
                type_ = param_value
            elif (param_value is not None and
                    param_value.lower() not in ["true", "false"] and
                    "variable" != block_type):
                # Basically: something that could be a valid variable name ;-)
                refs += re.findall(r'([a-zA-Z][\w\d]*)', param_value)

        return GNUBlock(
            block_type=block_type,
            name=name,
            value=value,
            label=label,
            type_=type_,
            refs=refs)

# TODO: Devise a better way to determine what the value "is", as a string-named
# type that would be appropriate for the ResourcePackager later since type 
# information is not available on the Flow Graph variable blocks.
__NUMERIC_TYPE_MAP = {
    int: 'long',
    long: 'long',
    float: 'float'
}
def string_to_value_type(value):
    type_ = None
    if type(value) != str:
        type_ = __NUMERIC_TYPE_MAP.get(type(value), 'double')
        value = value
    elif value.lower() == 'true':
        type_ = 'boolean'
        value = True
    elif value.lower() == 'false':
        type_ = 'boolean'
        value = False
    elif len([c for c in value if c.isalpha()]) > 1 or len(value) == 0:
        # Has several alphabet characters, treat it like a string.
        type_ = 'string'
    else:
        # Hopefully this is a number in a string...
        value = eval(value, {"__builtins__":None})
        type_ = __NUMERIC_TYPE_MAP.get(type(value), 'double')
    return (value, type_)

class XMLParsing(object):

    def __init__(self, file_name):
        self.tree = ET.parse(file_name)
        self.root = self.tree.getroot()
        self.block_array = None
        self.properties_array = None
        self.python_class_name = ""
        self.python_file_name = ""
        self.ports_array = None
        self.variable_names = None
        self.__parse()

    # ##########################################################################
    # This method is responsible for iterating through the provided XML file and
    # identifying the block type, name, value, label, data type, and references
    # if relevent. This information is stored in a 2D array with headers based
    # on found <block>s that are not the very first block occurrance (since that
    # first block only contains setup information). Empty fields are then filled
    # with the value, "None". Then, the array is converted into the namedTuple
    # type "GNUBlock" for easy iteration and will be used to populate xml files.
    # ##########################################################################
    def __parse(self):
        # Get the blocks of interest and convert them to GNUBlocks, if enabled.
        # Note, if the block is not enabled, the factory returns None.
        xml_blocks = self.root.findall('block')
        gnublocks = [b for b in (GNUBlock.from_xml(x) for x in xml_blocks) if b]

        # Iterate over the blocks and collect them into our sets
        self.block_array = []
        self.variable_names = []
        for gnublock in gnublocks:
            # The options block is only used for pulling in the class name.
            if "options" == gnublock.block_type:
                self.python_class_name = gnublock.name
                self.python_file_name = self.python_class_name + ".py"
                continue

            if "variable" == gnublock.block_type:
                self.variable_names.append(gnublock.name)

            # Store the block
            self.block_array.append(gnublock)

        self.__create_properties_array()
        self.__find_inout_types()

    # ##########################################################################
    # In this method, the namedTuple array (created by the above parse() method)
    # is iterated through to first find all "variable" type blocks. After a
    # "variable" block is found, the method then iterates through the same
    # namedTuple array a second time to determine if the block's name is
    # referenced by another block (and used as its value). If a variable is
    # found to be referenced by another block, it is added to the
    # properties_array class variable for later use in generating
    # SimpleProperties for the generated PRF file.
    # ##########################################################################
    def __create_properties_array(self):
        def var_type_and_ref_check(block):
            (block.value, block.type) = string_to_value_type(block.value)
            if 'string' == block.type:
                return any([v for v in self.variable_names if v in block.value])
            else:
                return False

        temp = set()
        for A, B in itertools.combinations(self.block_array, 2):
            if "variable" in A.block_type:
                if A.name in B.refs:
                    if not var_type_and_ref_check(A):
                        temp.add(A)
                if B.name in A.refs:
                    if not var_type_and_ref_check(B):
                        temp.add(B)
        self.properties_array = list(temp)

    # ##########################################################################
    # This method iterates through the current object's created "block_array"
    # searching for redhawk source and sink blocks. When found, select
    # information from these blocks are stored in a namedTuple and ultimately a
    # class list (in case of multiple instances). The new list is then analyzed
    # to determine the input/output "type" of the user's flowgraph and if the
    # provided flowgraph meets one input/output requirement.
    # ##########################################################################
    def __find_inout_types(self):
        integ_prefix = "redhawk_integration_rh_"
        self.ports_array = []
        for block in self.block_array:
            if block.block_type.startswith(integ_prefix):
                self.ports_array.append(
                    RHBlock(
                        name=block.name,
                        type=block.type,
                        direction=block.block_type.replace(integ_prefix,'')
                        )
                    )        
        if not self.ports_array:
            sys.exit("No REDHAWK source or sink block provided. Exiting.")

    # ##########################################################################
    # This method was added to provide quick testing capabilities for data
    # extraction from a user passed GRC file. Once the XMLParsing object is
    # created and XMLParsing.testing() is called, this method calls upon upon
    # all of the other methods in this class and outputs all relevent
    # information to a file named "testing_output.txt" in the current directory.
    # ##########################################################################
    def testing(self):

        f = open("testing_output.txt", "w")
        f.write("Output from iterating over current objects \"block_array\": \n")

        for block in self.block_array:
            f.write(str(block) + "\n")

        if self.properties_array:
            f.write("\nContents of the \"properties_array\" list: \n")
            for block in self.properties_array:
                f.write(str(block) + "\n")

        if self.ports_array:
            f.write("\nContents of \"ports_array\" list: \n")
            for item in self.ports_array:
                f.write(str(item) + "\n")

        f.close()
