from eprtool.stylecheck import *


def test_stylecheck():
    msgs = check("example.py", '''
"""Docstring"""
_ = """This one is OK. Was passiert jetzt? Is this not english?"""
__author__ = "12345, Nachname wrong"

# Das hier auf deutsch

a = "😭"

class X:
    """deutsch"""
    pass

for line in range(5):
    pass
'''.encode())
    #print("".join(repr(msg)+",\n" for msg in msgs))
    assert msgs == [
        Message(filename='example.py', line=4, column=14, end_line=4, end_column=37, source='pylint', msg_id='C3304', msg='Malformed __author__ value \'12345, Nachname wrong\' (format should be "1234567, Lastname" or "1234567, Lastname1, 7654321, Lastname2" for multiple authors)'),
        Message(filename='example.py', line=6, column=2, end_line=6, end_column=23, source='pylint', msg_id='C3322', msg='Likely German comment (confidence: 1.00)'),
        Message(filename='example.py', line=8, column=1, end_line=8, end_column=2, source='pylint', msg_id='C0103', msg='Constant name "a" doesn\'t conform to UPPER_CASE naming style'),
        Message(filename='example.py', line=10, column=1, end_line=None, end_column=None, source='pycodestyle', msg_id='E302', msg='expected 2 blank lines, found 1'),
        Message(filename='example.py', line=11, column=5, end_line=11, end_column=18, source='pylint', msg_id='C3321', msg='Likely German docstring (confidence: 1.00)'),
        Message(filename='example.py', line=12, column=5, end_line=12, end_column=9, source='pylint', msg_id='W0107', msg='Unnecessary pass statement'),
        Message(filename='example.py', line=14, column=1, end_line=None, end_column=None, source='pycodestyle', msg_id='E305', msg='expected 2 blank lines after class or function definition, found 1'),
    ]
