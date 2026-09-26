import sys, os, importlib

_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))                   
sys.path.insert(0, _parent)                                                             
__path__.append(_parent)

def __getattr__(name):
    mod = importlib.import_module(name)
    globals()[name] = mod
    return mod
