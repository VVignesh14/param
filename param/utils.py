import logging
from contextlib import contextmanager
from logging import DEBUG, INFO, WARNING, ERROR, CRITICAL, Logger
from inspect import getmro 
from typing import List
import textwrap

T = textwrap.TextWrapper(
    width = 80,
    expand_tabs=False,
    drop_whitespace = True,
    replace_whitespace= True,
    fix_sentence_endings = True,       
)

def wrap_error_text(text):
    # return T.wrap(text)
    #'\n'+'\n'.join([line.lstrip() 
    return textwrap.fill(
        text = textwrap.dedent(text).lstrip(),
        width = 80,
        initial_indent='\n',
        expand_tabs=True,
        drop_whitespace = True,
        replace_whitespace= True,
    )

VERBOSE = INFO - 1
logging.addLevelName(VERBOSE, "VERBOSE")
    
def get_logger(name : str = None) -> Logger:
    if name is None:
        root_logger = logging.getLogger('param')
        if not root_logger.handlers:
            root_logger.setLevel(logging.INFO)
            formatter = logging.Formatter(
                fmt='%(levelname)s:%(name)s: %(message)s')
            handler = logging.StreamHandler()
            handler.setFormatter(formatter)
            root_logger.addHandler(handler)
            return root_logger
    else:
        return logging.getLogger('param.' + name)


@contextmanager
def logging_level(level : int):
    """
    Temporarily modify param's logging level.
    """
    level = level.upper()
    levels = [DEBUG, INFO, WARNING, ERROR, CRITICAL, VERBOSE]
    level_names = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL', 'VERBOSE']

    if level not in level_names:
        raise Exception("Level %r not in %r" % (level, levels))

    param_logger = get_logger()
    logging_level = param_logger.getEffectiveLevel()
    param_logger.setLevel(levels[level_names.index(level)])
    try:
        yield None
    finally:
        param_logger.setLevel(logging_level)


def classlist(class_ : type) -> List[type]:
    """
    Return a list of the class hierarchy above (and including) the given class.

    Same as `inspect.getmro(class_)[::-1]`
    """
    return getmro(class_)[::-1]


def descendents(class_ : type) -> List[type]:
    """
    Return a list of the class hierarchy below (and including) the given class.

    The list is ordered from least- to most-specific.  Can be useful for
    printing the contents of an entire class hierarchy.
    """
    assert isinstance(class_,type)
    q = [class_]
    out = []
    while len(q):
        x = q.pop(0)
        out.insert(0,x)
        for b in x.__subclasses__():
            if b not in q and b not in out:
                q.append(b)
    return out[::-1]


__all__ = ['get_logger', 'logging_level', 'wrap_error_text', 'classlist', 'descendents']