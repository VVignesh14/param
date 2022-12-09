"""
Generic support for objects with full-featured Parameters and
messaging.

This file comes from the Param library (https://github.com/holoviz/param)
but can be taken out of the param module and used on its own if desired,
either alone (providing basic Parameter support) or with param's
__init__.py (providing specialized Parameter types).
"""

import copy
import datetime as dt
import re
import sys
import inspect
import random
import numbers
import operator
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, Union
from types import FunctionType, TracebackType
from collections import defaultdict, namedtuple, OrderedDict
from functools import partial, wraps, reduce
from operator import itemgetter, attrgetter
from contextlib import contextmanager
from logging import DEBUG, INFO, WARNING
from inspect import getfullargspec

try:
    # In case the optional ipython module is unavailable
    from .ipython import ParamPager
    param_pager = ParamPager(metaclass=True)  # Generates param description
except:
    param_pager = None

dt_types = (dt.datetime, dt.date)

try:
    import numpy as np
    dt_types = dt_types + (np.datetime64,)
except:
    pass

# Allow this file to be used standalone if desired, albeit without JSON serialization
try:
    from . import serializer
except ImportError:
    serializer = None

from .utils import *

basestring = basestring if sys.version_info[0]==2 else str # noqa: it is defined


# Get the appropriate logging.Logger instance. If `logger` is None, a
# logger named `"param"` will be instantiated. If `name` is set, a descendant
# logger with the name ``"param.<name>"`` is returned (or
# ``logger.name + ".<name>"``)
logger = None
# Indicates whether warnings should be raised as errors, stopping
# processing.
warnings_as_exceptions = False

docstring_signature = True        # Add signature to class docstrings
docstring_describe_params = True  # Add parameter description to class
                                  # docstrings (requires ipython module)
object_count = 0
warning_count = 0

class _Undefined:
    """
    Dummy value to signal completely undefined values rather than
    simple None values.
    """

@contextmanager
def _batch_call_watchers(parameterized, enable=True, run=True):
    """
    Internal version of batch_call_watchers, adding control over queueing and running.
    Only actually batches events if enable=True; otherwise a no-op. Only actually
    calls the accumulated watchers on exit if run=True; otherwise they remain queued.
    """
    BATCH_WATCH = parameterized.param._BATCH_WATCH
    parameterized.param._BATCH_WATCH = enable or parameterized.param._BATCH_WATCH
    try:
        yield
    finally:
        parameterized.param._BATCH_WATCH = BATCH_WATCH
        if run and not BATCH_WATCH:
            parameterized.param._batch_call_watchers()

batch_watch = _batch_call_watchers # PARAM2_DEPRECATION: Remove this compatibility alias for param 2.0 and later.

@contextmanager
def batch_call_watchers(parameterized):
    """
    Context manager to batch events to provide to Watchers on a
    parameterized object.  This context manager queues any events
    triggered by setting a parameter on the supplied parameterized
    object, saving them up to dispatch them all at once when the
    context manager exits.
    """
    BATCH_WATCH = parameterized.param._BATCH_WATCH
    parameterized.param._BATCH_WATCH = True
    try:
        yield
    finally:
        parameterized.param._BATCH_WATCH = BATCH_WATCH
        if not BATCH_WATCH:
            parameterized.param._batch_call_watchers()


@contextmanager
def edit_constant(parameterized):
    """
    Temporarily set parameters on Parameterized object to constant=False
    to allow editing them.
    """
    params = parameterized.param.objects('existing').values()
    constants = [p.constant for p in params]
    for p in params:
        p.constant = False
    try:
        yield
    except:
        raise
    finally:
        for (p, const) in zip(params, constants):
            p.constant = const


@contextmanager
def discard_events(parameterized):
    """
    Context manager that discards any events within its scope
    triggered on the supplied parameterized object.
    """
    batch_watch = parameterized.param._BATCH_WATCH
    parameterized.param._BATCH_WATCH = True
    watchers, events = (list(parameterized.param._watchers),
                        list(parameterized.param._events))
    try:
        yield
    except:
        raise
    finally:
        parameterized.param._BATCH_WATCH = batch_watch
        parameterized.param._watchers = watchers
        parameterized.param._events = events


# External components can register an async executor which will run
# async functions
async_executor = None


def classlist(class_):
    """
    Return a list of the class hierarchy above (and including) the given class.

    Same as `inspect.getmro(class_)[::-1]`
    """
    return inspect.getmro(class_)[::-1]


def descendents(class_):
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


def get_all_slots(class_):
    """
    Return a list of slot names for slots defined in `class_` and its
    superclasses.
    """
    # A subclass's __slots__ attribute does not contain slots defined
    # in its superclass (the superclass' __slots__ end up as
    # attributes of the subclass).
    all_slots = []
    parent_param_classes = [c for c in classlist(class_)[1::]]
    for c in parent_param_classes:
        if hasattr(c,'__slots__'):
            all_slots+=c.__slots__
    return all_slots


def get_occupied_slots(instance):
    """
    Return a list of slots for which values have been set.

    (While a slot might be defined, if a value for that slot hasn't
    been set, then it's an AttributeError to request the slot's
    value.)
    """
    return [slot for slot in get_all_slots(type(instance))
            if hasattr(instance,slot)]


def all_equal(arg1,arg2):
    """
    Return a single boolean for arg1==arg2, even for numpy arrays
    using element-wise comparison.

    Uses all(arg1==arg2) for sequences, and arg1==arg2 otherwise.

    If both objects have an '_infinitely_iterable' attribute, they are
    not be zipped together and are compared directly instead.
    """
    if all(hasattr(el, '_infinitely_iterable') for el in [arg1,arg2]):
        return arg1==arg2
    try:
        return all(a1 == a2 for a1, a2 in zip(arg1, arg2))
    except TypeError:
        return arg1==arg2


class bothmethod(object): # pylint: disable-msg=R0903
    """
    'optional @classmethod'

    A decorator that allows a method to receive either the class
    object (if called on the class) or the instance object
    (if called on the instance) as its first argument.

    Code (but not documentation) copied from:
    http://aspn.activestate.com/ASPN/Cookbook/Python/Recipe/523033.
    """
    # pylint: disable-msg=R0903

    def __init__(self, func):
        self.func = func

    # i.e. this is also a non-data descriptor
    def __get__(self, obj, type_=None):
        if obj is None:
            return wraps(self.func)(partial(self.func, type_))
        else:
            return wraps(self.func)(partial(self.func, obj))


def _getattrr(obj, attr, *args):
    def _getattr(obj, attr):
        return getattr(obj, attr, *args)
    return reduce(_getattr, [obj] + attr.split('.'))


def accept_arguments(f):
    """
    Decorator for decorators that accept arguments
    """
    @wraps(f)
    def _f(*args, **kwargs):
        return lambda actual_f: f(actual_f, *args, **kwargs)
    return _f


def no_instance_params(cls):
    """
    Disables instance parameters on the class
    """
    cls._disable_instance__params = True
    return cls


def iscoroutinefunction(function):
    """
    Whether the function is an asynchronous coroutine function.
    """
    if not hasattr(inspect, 'iscoroutinefunction'):
        return False
    import asyncio
    try:
        return (
            inspect.isasyncgenfunction(function) or
            asyncio.iscoroutinefunction(function)
        )
    except AttributeError:
        return False


def instance_descriptor(set : Callable[[Any, Any, Any],None]) -> Callable[[Any, Any, Any],None]:
    # If parameter has an instance Parameter, delegate setting
    def _set(self, obj, val):
        instance_param = getattr(obj, '_instance__params', {}).get(self.name)
        if instance_param is not None and self is not instance_param:
            instance_param.__set__(obj, val)
            return
        set(self, obj, val)
    return _set


def get_method_owner(method : Callable) -> Any:
    """
    Gets the instance that owns the supplied method
    """
    if not inspect.ismethod(method):
        return None
    if isinstance(method, partial):
        method = method.func
    return method.__self__ if sys.version_info.major >= 3 else method.im_self


@accept_arguments
def depends(func, *dependencies, **kw):
    """
    Annotates a function or Parameterized method to express its
    dependencies.  The specified dependencies can be either be
    Parameter instances or if a method is supplied they can be
    defined as strings referring to Parameters of the class,
    or Parameters of subobjects (Parameterized objects that are
    values of this object's parameters).  Dependencies can either be
    on Parameter values, or on other metadata about the Parameter.
    """

    # PARAM2_DEPRECATION: python2 workaround; python3 allows kw-only args
    # (i.e. "func, *dependencies, watch=False" rather than **kw and the check below)
    watch = kw.pop("watch", False)
    on_init = kw.pop("on_init", False)

    if iscoroutinefunction(func):
        from ._async import generate_depends
        _depends = generate_depends(func)
    else:
        @wraps(func)
        def _depends(*args, **kw):
            return func(*args, **kw)

    deps = list(dependencies)+list(kw.values())
    string_specs = False
    for dep in deps:
        if isinstance(dep, basestring):
            string_specs = True
        elif not isinstance(dep, Parameter):
            raise ValueError('The depends decorator only accepts string '
                             'types referencing a parameter or parameter '
                             'instances, found %s type instead.' %
                             type(dep).__name__)
        elif not (isinstance(dep.owner, Parameterized) or
                  (isinstance(dep.owner, ParameterizedMetaclass))):
            owner = 'None' if dep.owner is None else '%s class' % type(dep.owner).__name__
            raise ValueError('Parameters supplied to the depends decorator, '
                             'must be bound to a Parameterized class or '
                             'instance not %s.' % owner)

    if (any(isinstance(dep, Parameter) for dep in deps) and
        any(isinstance(dep, basestring) for dep in deps)):
        raise ValueError('Dependencies must either be defined as strings '
                         'referencing parameters on the class defining '
                         'the decorated method or as parameter instances. '
                         'Mixing of string specs and parameter instances '
                         'is not supported.')
    elif string_specs and kw:
        raise AssertionError('Supplying keywords to the decorated method '
                             'or function is not supported when referencing '
                             'parameters by name.')

    if not string_specs and watch: # string_specs case handled elsewhere (later), in Parameterized.__init__
        if iscoroutinefunction(func):
            from ._async import generate_callback
            cb = generate_callback(func, dependencies, kw)
        else:
            def cb(*events):
                args = (getattr(dep.owner, dep.name) for dep in dependencies)
                dep_kwargs = {n: getattr(dep.owner, dep.name) for n, dep in kw.items()}
                return func(*args, **dep_kwargs)

        grouped = defaultdict(list)
        for dep in deps:
            grouped[id(dep.owner)].append(dep)
        for group in grouped.values():
            group[0].owner.param.watch(cb, [dep.name for dep in group])

    _dinfo = getattr(func, '_dinfo', {})
    _dinfo.update({'dependencies': dependencies,
                   'kw': kw, 'watch': watch, 'on_init': on_init})

    _depends._dinfo = _dinfo

    return _depends


@accept_arguments
def output(func, *output, **kw):
    """
    output allows annotating a method on a Parameterized class to
    declare that it returns an output of a specific type. The outputs
    of a Parameterized class can be queried using the
    Parameterized.param.outputs method. By default the output will
    inherit the method name but a custom name can be declared by
    expressing the Parameter type using a keyword argument. Declaring
    multiple return types using keywords is only supported in Python >= 3.6.

    The simplest declaration simply declares the method returns an
    object without any type guarantees, e.g.:

      @output()

    If a specific parameter type is specified this is a declaration
    that the method will return a value of that type, e.g.:

      @output(param.Number())

    To override the default name of the output the type may be declared
    as a keyword argument, e.g.:

      @output(custom_name=param.Number())

    Multiple outputs may be declared using keywords mapping from
    output name to the type for Python >= 3.6 or using tuples of the
    same format, which is supported for earlier versions, i.e. these
    two declarations are equivalent:

      @output(number=param.Number(), string=param.String())

      @output(('number', param.Number()), ('string', param.String()))

    output also accepts Python object types which will be upgraded to
    a ClassSelector, e.g.:

      @output(int)
    """
    if output:
        outputs = []
        for i, out in enumerate(output):
            i = i if len(output) > 1 else None
            if isinstance(out, tuple) and len(out) == 2 and isinstance(out[0], str):
                outputs.append(out+(i,))
            elif isinstance(out, str):
                outputs.append((out, Parameter(), i))
            else:
                outputs.append((None, out, i))
    elif kw:
        py_major = sys.version_info.major
        py_minor = sys.version_info.minor
        if (py_major < 3 or (py_major == 3 and py_minor < 6)) and len(kw) > 1:
            raise ValueError('Multiple output declaration using keywords '
                             'only supported in Python >= 3.6.')
          # (requires keywords to be kept ordered, which was not true in previous versions)
        outputs = [(name, otype, i if len(kw) > 1 else None)
                   for i, (name, otype) in enumerate(kw.items())]
    else:
        outputs = [(None, Parameter(), None)]

    names, processed = [], []
    for name, otype, i in outputs:
        if isinstance(otype, type):
            if issubclass(otype, Parameter):
                otype = otype()
            else:
                from .import ClassSelector
                otype = ClassSelector(class_=otype)
        elif isinstance(otype, tuple) and all(isinstance(t, type) for t in otype):
            from .import ClassSelector
            otype = ClassSelector(class_=otype)
        if not isinstance(otype, Parameter):
            raise ValueError('output type must be declared with a Parameter class, '
                             'instance or a Python object type.')
        processed.append((name, otype, i))
        names.append(name)

    if len(set(names)) != len(names):
        raise ValueError('When declaring multiple outputs each value '
                         'must be unique.')

    _dinfo = getattr(func, '_dinfo', {})
    _dinfo.update({'outputs': processed})

    @wraps(func)
    def _output(*args,**kw):
        return func(*args,**kw)

    _output._dinfo = _dinfo

    return _output


def _parse_dependency_spec(spec):
    """
    Parses param.depends specifications into three components:

    1. The dotted path to the sub-object
    2. The attribute being depended on, i.e. either a parameter or method
    3. The parameter attribute being depended on
    """
    assert spec.count(":")<=1
    spec = spec.strip()
    m = re.match("(?P<path>[^:]*):?(?P<what>.*)", spec)
    what = m.group('what')
    path = "."+m.group('path')
    m = re.match(r"(?P<obj>.*)(\.)(?P<attr>.*)", path)
    obj = m.group('obj')
    attr = m.group("attr")
    return obj or None, attr, what or 'value'


def _params_depended_on(minfo, dynamic=True, intermediate=True):
    """
    Resolves dependencies declared on a Parameterized method.
    Dynamic dependencies, i.e. dependencies on sub-objects which may
    or may not yet be available, are only resolved if dynamic=True.
    By default intermediate dependencies, i.e. dependencies on the
    path to a sub-object are returned. For example for a dependency
    on 'a.b.c' dependencies on 'a' and 'b' are returned as long as
    intermediate=True.

    Returns lists of concrete dependencies on available parameters
    and dynamic dependencies specifications which have to resolved
    if the referenced sub-objects are defined.
    """
    deps, dynamic_deps = [], []
    dinfo = getattr(minfo.method, "_dinfo", {})
    for d in dinfo.get('dependencies', list(minfo.cls.param)):
        ddeps, ddynamic_deps = (minfo.inst or minfo.cls).param._spec_to_obj(d, dynamic, intermediate)
        dynamic_deps += ddynamic_deps
        for dep in ddeps:
            if isinstance(dep, PInfo):
                deps.append(dep)
            else:
                method_deps, method_dynamic_deps = _params_depended_on(dep, dynamic, intermediate)
                deps += method_deps
                dynamic_deps += method_dynamic_deps
    return deps, dynamic_deps


def _resolve_mcs_deps(obj, resolved, dynamic, intermediate=True) -> List[Union['PInfo', 'MInfo']]:
    """
    Resolves constant and dynamic parameter dependencies previously
    obtained using the _params_depended_on function. Existing resolved
    dependencies are updated with a supplied parameter instance while
    dynamic dependencies are resolved if possible.
    """
    dependencies = []
    for dep in resolved:
        if not issubclass(type(obj), dep.cls):
            dependencies.append(dep)
            continue
        inst = obj if dep.inst is None else dep.inst
        dep = PInfo(inst=inst, cls=dep.cls, name=dep.name,
                    pobj=inst.param[dep.name], what=dep.what)
        dependencies.append(dep)
    for dep in dynamic:
        subresolved, _ = obj.param._spec_to_obj(dep.spec, intermediate=intermediate)
        for subdep in subresolved:
            if isinstance(subdep, PInfo):
                dependencies.append(subdep)
            else:
                dependencies += _params_depended_on(subdep, intermediate=intermediate)[0]
    return dependencies


def _skip_event(*events, **kwargs):
    """
    Checks whether a subobject event should be skipped.
    Returns True if all the values on the new subobject
    match the values on the previous subobject.
    """
    what = kwargs.get('what', 'value')
    changed = kwargs.get('changed')
    if changed is None:
        return False
    for e in events:
        for p in changed:
            if what == 'value':
                old = _Undefined if e.old is None else _getattrr(e.old, p, None)
                new = _Undefined if e.new is None else _getattrr(e.new, p, None)
            else:
                old = _Undefined if e.old is None else _getattrr(e.old.param[p], what, None)
                new = _Undefined if e.new is None else _getattrr(e.new.param[p], what, None)
            if not Comparator.is_equal(old, new):
                return False
    return True


def _m_caller(self, method_name, what='value', changed=None, callback=None):
    """
    Wraps a method call adding support for scheduling a callback
    before it is executed and skipping events if a subobject has
    changed but its values have not.
    """
    function = getattr(self, method_name)
    if iscoroutinefunction(function):
        from ._async import generate_caller
        caller = generate_caller(function, what=what, changed=changed, callback=callback, skip_event=_skip_event)
    else:
        def caller(*events):
            if callback: callback(*events)
            if not _skip_event(*events, what=what, changed=changed):
                return function()
    caller._watcher_name = method_name
    return caller


def _add_doc(obj, docstring):
    """Add a docstring to a namedtuple, if on python3 where that's allowed"""
    if sys.version_info[0]>2:
        obj.__doc__ = docstring


PInfo = namedtuple("PInfo", "inst cls name pobj what"); _add_doc(PInfo,
    """
    Object describing something being watched about a Parameter.

    `inst`: Parameterized instance owning the Parameter, or None

    `cls`: Parameterized class owning the Parameter

    `name`: Name of the Parameter being watched

    `pobj`: Parameter object being watched

    `what`: What is being watched on the Parameter (either 'value' or a slot name)
    """)

MInfo = namedtuple("MInfo", "inst cls name method"); _add_doc(MInfo,
    """
    Object describing a Parameterized method being watched.

    `inst`: Parameterized instance owning the method, or None

    `cls`: Parameterized class owning the method

    `name`: Name of the method being watched

    `method`: bound method of the object being watched
    """)

DInfo = namedtuple("DInfo", "spec"); _add_doc(DInfo,
    """
    Object describing dynamic dependencies.
    `spec`: Dependency specification to resolve
    """)

Event = namedtuple("Event", "what name obj cls old new type"); _add_doc(Event,
    """
    Object representing an event that triggers a Watcher.

    `what`: What is being watched on the Parameter (either value or a slot name)

    `name`: Name of the Parameter that was set or triggered

    `obj`: Parameterized instance owning the watched Parameter, or None

    `cls`: Parameterized class owning the watched Parameter

    `old`: Previous value of the item being watched

    `new`: New value of the item being watched

    `type`: `triggered` if this event was triggered explicitly), `changed` if
    the item was set and watching for `onlychanged`, `set` if the item was set,
    or  None if type not yet known
    """)

_Watcher = namedtuple("Watcher", "inst cls fn mode onlychanged parameter_names what queued precedence")

class Watcher(_Watcher):
    """
    Object declaring a callback function to invoke when an Event is
    triggered on a watched item.

    `inst`: Parameterized instance owning the watched Parameter, or
    None

    `cls`: Parameterized class owning the watched Parameter

    `fn`: Callback function to invoke when triggered by a watched
    Parameter

    `mode`: 'args' for param.watch (call `fn` with PInfo object
    positional args), or 'kwargs' for param.watch_values (call `fn`
    with <param_name>:<new_value> keywords)

    `onlychanged`: If True, only trigger for actual changes, not
    setting to the current value

    `parameter_names`: List of Parameters to watch, by name

    `what`: What to watch on the Parameters (either 'value' or a slot
    name)

    `queued`: Immediately invoke callbacks triggered during processing
            of an Event (if False), or queue them up for processing
            later, after this event has been handled (if True)

    `precedence`: A numeric value which determines the precedence of
                  the watcher.  Lower precedence values are executed
                  with higher priority.
    """

    def __new__(cls_, *args, **kwargs):
        """
        Allows creating Watcher without explicit precedence value.
        """
        values = dict(zip(cls_._fields, args))
        values.update(kwargs)
        if 'precedence' not in values:
            values['precedence'] = 0
        return super(Watcher, cls_).__new__(cls_, **values)

    def __iter__(self):
        """
        Backward compatibility layer to allow tuple unpacking without
        the precedence value. Important for Panel which creates a
        custom Watcher and uses tuple unpacking. Will be dropped in
        Param 3.x.
        """
        return iter(self[:-1])

    def __str__(self):
        cls = type(self)
        attrs = ', '.join(['%s=%r' % (f, getattr(self, f)) for f in cls._fields])
        return "{cls}({attrs})".format(cls=cls.__name__, attrs=attrs)




class ParameterMetaclass(type):
    """
    Metaclass allowing control over creation of Parameter classes.
    """
    def __new__(mcs, classname : str, bases : Tuple[Any], classdict : Dict) -> 'ParameterMetaclass':

        # store the class's docstring in __classdoc
        if '__doc__' in classdict:
            classdict['__classdoc']=classdict['__doc__']

        # when asking for help on Parameter *object*, return the doc slot
        classdict['__doc__']=property(attrgetter('doc'))

        # To get the benefit of slots, subclasses must themselves define
        # __slots__, whether or not they define attributes not present in
        # the base Parameter class.  That's because a subclass will have
        # a __dict__ unless it also defines __slots__.
        if '__slots__' not in classdict:
            classdict['__slots__']=[]

        # No special handling for a __dict__ slot; should there be?
        return type.__new__(mcs, classname, bases, classdict)

    def __getattribute__(mcs, name : str) -> Any:
        if name=='__doc__':
            # when asking for help on Parameter *class*, return the
            # stored class docstring
            return type.__getattribute__(mcs,'__classdoc')
        else:
            return type.__getattribute__(mcs,name)



class Parameter(metaclass=ParameterMetaclass):
    """
    An attribute descriptor for declaring parameters.

    Parameters are a special kind of class attribute.  Setting a
    Parameterized class attribute to be a Parameter instance causes
    that attribute of the class (and the class's instances) to be
    treated as a Parameter.  This allows special behavior, including
    dynamically generated parameter values, documentation strings,
    constant and read-only parameters, and type or range checking at
    assignment time.

    For example, suppose someone wants to define two new kinds of
    objects Foo and Bar, such that Bar has a parameter delta, Foo is a
    subclass of Bar, and Foo has parameters alpha, sigma, and gamma
    (and delta inherited from Bar).  She would begin her class
    definitions with something like this::

       class Bar(Parameterized):
           delta = Parameter(default=0.6, doc='The difference between steps.')
           ...
       class Foo(Bar):
           alpha = Parameter(default=0.1, doc='The starting value.')
           sigma = Parameter(default=0.5, doc='The standard deviation.',
                           constant=True)
           gamma = Parameter(default=1.0, doc='The ending value.')
           ...

    Class Foo would then have four parameters, with delta defaulting
    to 0.6.

    Parameters have several advantages over plain attributes:

    1. Parameters can be set automatically when an instance is
       constructed: The default constructor for Foo (and Bar) will
       accept arbitrary keyword arguments, each of which can be used
       to specify the value of a Parameter of Foo (or any of Foo's
       superclasses).  E.g., if a script does this::

           myfoo = Foo(alpha=0.5)

       myfoo.alpha will return 0.5, without the Foo constructor
       needing special code to set alpha.

       If Foo implements its own constructor, keyword arguments will
       still be accepted if the constructor accepts a dictionary of
       keyword arguments (as in ``def __init__(self,**params):``), and
       then each class calls its superclass (as in
       ``super(Foo,self).__init__(**params)``) so that the
       Parameterized constructor will process the keywords.

    2. A Parameterized class need specify only the attributes of a
       Parameter whose values differ from those declared in
       superclasses; the other values will be inherited.  E.g. if Foo
       declares::

        delta = Parameter(default=0.2)

       the default value of 0.2 will override the 0.6 inherited from
       Bar, but the doc will be inherited from Bar.

    3. The Parameter descriptor class can be subclassed to provide
       more complex behavior, allowing special types of parameters
       that, for example, require their values to be numbers in
       certain ranges, generate their values dynamically from a random
       distribution, or read their values from a file or other
       external source.

    4. The attributes associated with Parameters provide enough
       information for automatically generating property sheets in
       graphical user interfaces, allowing Parameterized instances to
       be edited by users.

    Note that Parameters can only be used when set as class attributes
    of Parameterized classes. Parameters used as standalone objects,
    or as class attributes of non-Parameterized classes, will not have
    the behavior described here.
    """

    # Be careful when referring to the 'name' of a Parameter:
    #
    # * A Parameterized class has a name for the attribute which is
    #   being represented by the Parameter in the code, 
    #   this is called the 'attrib_name'.
    #
    # * When a Parameterized instance has its own local value for a
    #   parameter, it is stored as '_X_param_value' (where X is the
    #   attrib_name for the Parameter); in the code, this is called
    #   the internal_name.


    # So that the extra features of Parameters do not require a lot of
    # overhead, Parameters are implemented using __slots__ (see
    # http://www.python.org/doc/2.4/ref/slots.html). 

    __slots__ = ['owner', 'name', '_internal_name', 'default', 'doc',
                 'precedence', '_deep_copy', 'constant', 'readonly',
                 'pickle_default_value', 'allow_None', 'per_instance',
                 'watchers', '_label', 'class_member']

    # Note: When initially created, a Parameter does not know which
    # Parameterized class owns it. Once the owning Parameterized
    # class is created, owner, name, and _internal_name are
    # set.

    _serializers = {'json': serializer.JSONSerialization}

    def __init__(self, default : Any = None, doc : str = None,
                 constant : bool = False, readonly : bool = False, allow_None : bool = False,
                 label : str = None,  per_instance : bool = True, deep_copy : bool = False, 
                 class_member : bool = False, pickle_default_value : bool = True, precedence : float = None):  # pylint: disable-msg=R0913

        """Initialize a new Parameter object and store the supplied attributes:

        default: the owning class's value for the attribute represented
        by this Parameter, which can be overridden in an instance.

        doc: docstring explaining what this parameter represents.

        constant: if true, the Parameter value can be changed only at
        the class level or in a Parameterized constructor call. The
        value is otherwise constant on the Parameterized instance,
        once it has been constructed.

        readonly: if true, the Parameter value cannot ordinarily be
        changed by setting the attribute at the class or instance
        levels at all. The value can still be changed in code by
        temporarily overriding the value of this slot and then
        restoring it, which is useful for reporting values that the
        _user_ should never change but which do change during code
        execution.

        allow_None: if True, None is accepted as a valid value for
        this Parameter, in addition to any other values that are
        allowed. If the default value is defined as None, allow_None
        is set to True automatically.

        label: optional text label to be used when this Parameter is
        shown in a listing. If no label is supplied, the attribute name
        for this parameter in the owning Parameterized object is used.

        per_instance: whether a separate Parameter instance will be
        created for every Parameterized instance. True by default.
        If False, all instances of a Parameterized class will share
        the same Parameter object, including all validation
        attributes (bounds, etc.). See also deep_copy, which is
        conceptually similar but affects the Parameter value rather
        than the Parameter object.

        deep_copy: controls whether the value of this Parameter will
        be deepcopied when a Parameterized object is instantiated (if
        True), or if the single default value will be shared by all
        Parameterized instances (if False). For an immutable Parameter
        value, it is best to leave deep_copy at the default of
        False, so that a user can choose to change the value at the
        Parameterized instance level (affecting only that instance) or
        at the Parameterized class or superclass level (affecting all
        existing and future instances of that class or superclass). For
        a mutable Parameter value, the default of False is also appropriate
        if you want all instances to share the same value state, e.g. if
        they are each simply referring to a single global object like
        a singleton. If instead each Parameterized should have its own
        independently mutable value, deep_copy should be set to
        True, but note that there is then no simple way to change the
        value of this Parameter at the class or superclass level,
        because each instance, once created, will then have an
        independently deepcopied value.

        class_member : To make a ... 

        pickle_default_value: whether the default value should be
        pickled. Usually, you would want the default value to be pickled,
        but there are rare cases where that would not be the case (e.g.
        for file search paths that are specific to a certain system).

        precedence: a numeric value, usually in the range 0.0 to 1.0,
        which allows the order of Parameters in a class to be defined in
        a listing or e.g. in GUI menus. A negative precedence indicates
        a parameter that should be hidden in such listings.

        default, doc, and precedence all default to None, which allows
        inheritance of Parameter slots (attributes) from the owning-class'
        class hierarchy (see ParameterizedMetaclass).
        """
        self.owner = None
        self.name = None
        self._internal_name = None
        self.default = default
        self.doc = doc
        self.constant = constant # readonly is constant however readonly is dealt separately
        self.readonly = readonly
        self.allow_None = (default is None or allow_None)
        self._label = label
        self.per_instance = per_instance
        self.deep_copy = deep_copy
        self.class_member = class_member
        self.pickle_default_value = pickle_default_value
        self.precedence = precedence
        self.watchers = {}

    def __set_name__(self, owner : Union['Parameterized', Any], attrib_name : str) -> None:
        if self.name is not None:
            raise AttributeError('The %s parameter %r has already been '
                                 'assigned a name by the %s class, '
                                 'could not assign new name %r. Parameters '
                                 'may not be shared by multiple classes; '
                                 'ensure that you create a new parameter '
                                 'instance for each new class.'
                                 % (type(self).__name__, self.name,
                                    owner, attrib_name))
        self.name = attrib_name
        self._internal_name = "_%s_param_value" % attrib_name
        
    def __post_owner_init(self):
        self.__validate_slots()
        self.validate(self.default)

    def __validate_slots(self):
        if self.constant and self.allow_None:
            raise ValueError('constant values cannot be allowed to be None.')
        
    @classmethod
    def serialize(cls, value : Any) -> Any:
        "Given the parameter value, return a Python value suitable for serialization"
        return value

    @classmethod
    def deserialize(cls, value : Any) -> Any:
        "Given a serializable Python value, return a value that the parameter can be set to"
        return value

    def schema(self, safe : bool = False, subset=None, mode : str = 'json') -> Dict[str, Any]:
        if serializer is None:
            raise ImportError('Cannot import serializer.py needed to generate schema')
        if mode not in  self._serializers:
            raise KeyError('Mode %r not in available serialization formats %r'
                           % (mode, list(self._serializers.keys())))
        return self._serializers[mode].param_schema(self.__class__.__name__, self,
                                                    safe=safe, subset=subset)

    @property
    def label(self) -> str:
        if self.name and self._label is None:
            return label_formatter(self.name)
        else:
            return self._label

    @label.setter
    def label(self, val : str) -> None:
        self._label = val

    @property
    def deep_copy(self) -> bool:
        return self._deep_copy
    
    @deep_copy.setter
    def deep_copy(self, deep_copy : bool) -> None:
        """Constant parameters must be deep_copied."""
        # deep_copy doesn't actually matter for read-only
        # parameters, since they can't be set even on a class.  But
        # having this code avoids needless instantiation.
        if self.readonly:
            self._deep_copy = False
        else:
            self._deep_copy = deep_copy or self.constant # pylint: disable-msg=W0201

    def __setattr__(self, attribute : str, value : Any) -> None:
        if attribute == 'name' or getattr(self, 'name', None) and value != self.name:
            raise AttributeError("Parameter name cannot be modified after "
                                 "it has been bound to a Parameterized.")

        watched = (attribute != "default" and hasattr(self, 'watchers') and attribute in self.watchers)
        slot_attribute = attribute in self.__slots__
        try:
            old = getattr(self, attribute) if watched else not watched
        except AttributeError as exc:
            if slot_attribute:
                # If Parameter slot is defined but an AttributeError was raised
                # we are in __setstate__ and watchers should not be triggered
                old = not watched
            else:
                raise exc

        super(Parameter, self).__setattr__(attribute, value)

        if slot_attribute:
            self._on_slot_set(attribute, old, value)

        if old is not watched or not isinstance(self.owner, Parameterized):
            return

        event = Event(what=attribute, name=self.name, obj=None, cls=self.owner,
                      old=old, new=value, type=None)
        for watcher in self.watchers[attribute]:
            self.owner.param._call_watcher(watcher, event)
        if not self.owner.param._BATCH_WATCH:
            self.owner.param._batch_call_watchers()

    def _on_slot_set(self, attribute : str, old : Any, value : Any) -> None:
        """
        Can be overridden on subclasses to handle changes when parameter
        attribute is set.
        """

    def __get__(self, obj : Union['Parameterized', Any], objtype : Union['ParameterizedMetaclass', Any]): # pylint: disable-msg=W0613
        """
        Return the value for this Parameter.

        If called for a Parameterized class, produce that
        class's value (i.e. this Parameter object's 'default'
        attribute).

        If called for a Parameterized instance, produce that
        instance's value, if one has been set - otherwise produce the
        class's value (default).
        """        
        return obj.__dict__.get(self._internal_name, self.default)

    @instance_descriptor
    def __set__(self, obj : Union['Parameterized', Any], val : Any) -> None:
        """
        Set the value for this Parameter.

        If called for a Parameterized class, set that class's
        value (i.e. set this Parameter object's 'default' attribute).

        If called for a Parameterized instance, set the value of
        this Parameter on that instance (i.e. in the instance's
        __dict__, under the parameter's internal_name).

        If the Parameter's constant attribute is True, only allows
        the value to be set for a Parameterized class or on
        uninitialized Parameterized instances.

        If the Parameter's readonly attribute is True, only allows the
        value to be specified in the Parameter declaration inside the
        Parameterized source code. A read-only parameter also
        cannot be set on a Parameterized class.

        Note that until we support some form of read-only
        object, it is still possible to change the attributes of the
        object stored in a constant or read-only Parameter (e.g. one
        item in a list).
        """
        if self.readonly:
            raise TypeError("Read-only parameter '%s' cannot be set/modified" % self.name)
        
        self.validate(val)

        owner = obj if not self.class_member else self.owner

        _old = NotImplemented
        if self.constant:
            if self.default is None and owner.__dict__.get(self._internal_name, None) is None: 
                _old = None
            else:
                _old = owner.__dict__.get(self._internal_name, self.default)
                if val is not _old:
                    raise TypeError("Constant parameter '%s' cannot be modified"%self.name)
        else:
            _old = owner.__dict__.get(self._internal_name, self.default)

        # obj can be None if __set__ is called for a Parameterized class
        owner.__dict__[self._internal_name] = val
        
        self._post_setter(owner, val) 

        if not isinstance(owner, (Parameterized, ParameterizedMetaclass)) or issubclass(owner, (Parameterized, ParameterizedMetaclass)):
            """
            dont deal with events, watchers etc when object
            is not a Parameterized class child
            Many other variables like obj.param below 
            will also raise AttributeError
            """
            return           
        if not getattr(obj, 'initialized', False):
            return
        # obj.param._update_deps(self.name)

        # if obj is None:
        #     watchers = self.watchers.get("value")
        # elif hasattr(obj, '_param_watchers') and self.name in obj._param_watchers:
        #     watchers = obj._param_watchers[self.name].get('value')
        #     if watchers is None:
        #         watchers = self.watchers.get("value")
        # else:
        #     watchers = None

        # obj = self.owner if obj is None else obj

        # if obj is None or not watchers:
        #     return

        # event = Event(what='value', name=self.name, obj=obj, cls=self.owner,
        #               old=_old, new=val, type=None)

        # # Copy watchers here since they may be modified inplace during iteration
        # for watcher in sorted(watchers, key=lambda w: w.precedence):
        #     obj.param._call_watcher(watcher, event)
        # if not obj.param._BATCH_WATCH:
        #     obj.param._batch_call_watchers()

    def _validate_value(self, value : Any, allow_None : bool) -> None:
        """Implements validation for parameter value"""

    def adapt(self, val : Any) -> Any:
        """
        modify the given value if a proper logical reasoning can be given.
        returns modified value. Should not be mostly used unless the data stored is quite complex by structure.
        """
        return val

    def validate(self, val : Any) -> None:
        """Implements validation for the parameter value and attributes"""
        val = self.adapt(val)
        self._validate_value(val, self.allow_None)

    @property 
    def validator(self) -> FunctionType:
        return partial(self.validate, self=self)

    def _post_setter(self, owner : Union['Parameterized', Any], val : Any) -> None:
        """Called after the parameter value has been validated and set"""

    def __delete__(self, obj : Union['Parameterized', Any]):
        raise TypeError("Cannot delete '%s': Parameters deletion not allowed." % self.name)

    def __getstate__(self):
        """
        All Parameters have slots, not a dict, so we have to support
        pickle and deepcopy ourselves.
        """
        state = {}
        for slot in get_occupied_slots(self):
            state[slot] = getattr(self,slot)
        return state

    def __setstate__(self, state : Dict):
        """
        set values of __slots__ (instead of in non-existent __dict__)
        """
        # Handle renamed slots introduced for instance params
        if '_attrib_name' in state:
            state['name'] = state.pop('_attrib_name')
        if '_owner' in state:
            state['owner'] = state.pop('_owner')
        if 'watchers' not in state:
            state['watchers'] = {}
        if 'per_instance' not in state:
            state['per_instance'] = False
        if '_label' not in state:
            state['_label'] = None

        for (k,v) in state.items():
            setattr(self,k,v)



# Define one particular type of Parameter that is used in this file
class String(Parameter):
    """
    A String Parameter, with a default value and optional regular expression (regex) matching.

    Example of using a regex to implement IPv4 address matching::

      class IPAddress(String):
        '''IPv4 address as a string (dotted decimal notation)'''
       def __init__(self, default="0.0.0.0", allow_None=False, **kwargs):
           ip_regex = r'^((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$'
           super(IPAddress, self).__init__(default=default, regex=ip_regex, **kwargs)

    """

    __slots__ = ['regex']

    def __init__(self, default="", regex=None, allow_None=False, **kwargs):
        self.regex = regex
        super(String, self).__init__(default=default, allow_None=allow_None, **kwargs)
    
    def _validate_regex(self, val, regex):
        if (val is None and self.allow_None):
            return
        if regex is not None:
            match = re.match(regex, val) 
            if match is None:
                raise ValueError("String parameter %r value %r does not match regex %r."
                             % (self.name, val, regex))
            elif match.group(0) != val:
                raise ValueError("String parameter %r value %r does not match regex %r."
                             % (self.name, val, regex))
            
    def _validate_value(self, val, allow_None):
        if allow_None and val is None:
            return
        if not isinstance(val, basestring):
            raise ValueError("String parameter %r only takes a string value, "
                             "not value of type %s." % (self.name, type(val)))

    def validate(self, val):
        self._validate_value(val, self.allow_None)
        self._validate_regex(val, self.regex)


class shared_parameters(object):
    """
    Context manager to share parameter instances when creating
    multiple Parameterized objects of the same type. Parameter default
    values are deepcopied once and cached to be reused when another
    Parameterized object of the same type is instantiated.
    Can be useful to easily modify large collections of Parameterized
    objects at once and can provide a significant speedup.
    """

    _share : bool = False
    _shared_cache : Dict[str, Any] = {}

    def __enter__(self):
        shared_parameters._share = True

    def __exit__(self, exc_type : Optional[Type[BaseException]], exc_val : Optional[Type[BaseException]],
                         exc_tb : Optional[Type[TracebackType]]) -> bool:
        shared_parameters._share = False
        shared_parameters._shared_cache = {}


def as_uninitialized(fn):
    """
    Decorator: call fn with the parameterized_instance's
    initialization flag set to False, then revert the flag.

    (Used to decorate Parameterized methods that must alter
    a constant Parameter.)
    """
    @wraps(fn)
    def override_initialization(self_,*args,**kw):
        parameterized_instance = self_.self
        original_initialized = parameterized_instance.initialized
        parameterized_instance.initialized = False
        fn(parameterized_instance, *args, **kw)
        parameterized_instance.initialized = original_initialized
    return override_initialization


class Comparator(object):
    """
    Comparator defines methods for determining whether two objects
    should be considered equal. It works by registering custom
    comparison functions, which may either be registed by type or with
    a predicate function. If no matching comparison can be found for
    the two objects the comparison will return False.

    If registered by type the Comparator will check whether both
    objects are of that type and apply the comparison. If the equality
    function is instead registered with a function it will call the
    function with each object individually to check if the comparison
    applies. This is useful for defining comparisons for objects
    without explicitly importing them.

    To use the Comparator simply call the is_equal function.
    """

    equalities = {
        numbers.Number: operator.eq,
        basestring: operator.eq,
        bytes: operator.eq,
        type(None): operator.eq,
    }
    equalities.update({dtt: operator.eq for dtt in dt_types})

    @classmethod
    def is_equal(cls, obj1 : Any, obj2 : Any) -> bool:
        for eq_type, eq in cls.equalities.items():
            if ((isinstance(eq_type, FunctionType)
                 and eq_type(obj1) and eq_type(obj2))
                or (isinstance(obj1, eq_type) and isinstance(obj2, eq_type))):
                return eq(obj1, obj2)
        if isinstance(obj2, (list, set, tuple)):
            return cls.compare_iterator(obj1, obj2)
        elif isinstance(obj2, dict):
            return cls.compare_mapping(obj1, obj2)
        return False

    @classmethod
    def compare_iterator(cls, obj1 : Any, obj2 : Any) -> bool:
        if type(obj1) != type(obj2) or len(obj1) != len(obj2):
            return False
        for o1, o2 in zip(obj1, obj2):
            if not cls.is_equal(o1, o2):
                return False
        return True

    @classmethod
    def compare_mapping(cls, obj1 : Any, obj2 : Any) -> bool:
        if type(obj1) != type(obj2) or len(obj1) != len(obj2): return False
        for k in obj1:
            if k in obj2:
                if not cls.is_equal(obj1[k], obj2[k]):
                    return False
            else:
                return False
        return True


class ClassParameters(object):
    """Object that holds the namespace and implementation of Parameterized
    methods as well as any state that is not in __slots__ or the
    Parameters themselves.

    Exists at both the metaclass level (instantiated by the metaclass)
    and at the instance level. Can contain state specific to either the
    class or the instance as necessary.
    """

    _disable_stubs = False # Flag used to disable stubs in the API1 tests
                          # None for no action, True to raise and False to warn.

    def __init__(self, owner_cls : 'ParameterizedMetaclass') -> None:
        """
        cls is the Parameterized class which is always set.
        self is the instance if set.
        """
        self.owner_cls = owner_cls         
        self._parameters_state = {
            "BATCH_WATCH": False, # If true, Event and watcher objects are queued.
            "TRIGGER": False,
            "events": [], # Queue of batched events
            "watchers": [] # Queue of batched watchers
        }

    @property
    def _BATCH_WATCH(self):
        return self._parameters_state['BATCH_WATCH']

    @_BATCH_WATCH.setter
    def _BATCH_WATCH(self, value):
        self._parameters_state['BATCH_WATCH'] = value

    @property
    def _TRIGGER(self):
        return self._parameters_state['TRIGGER']

    @_TRIGGER.setter
    def _TRIGGER(self, value):
        self._parameters_state['TRIGGER'] = value

    @property
    def _events(self):
        return self._parameters_state['events']

    @_events.setter
    def _events(self, value):
        self._parameters_state['events'] = value

    @property
    def _watchers(self):
        return self._parameters_state['watchers']

    @_watchers.setter
    def _watchers(self, value):
        self._parameters_state['watchers'] = value

    @property
    def watchers(self):
        """Read-only list of watchers on this Parameterized"""
        return self._watchers

    def __setstate__(self, state):
        # Set old parameters state on Parameterized._parameters_state
        self_or_cls = state.get('self', state.get('cls'))
        for k in self_or_cls._parameters_state:
            key = '_'+k
            if key in state:
                self_or_cls._parameters_state[k] = state.pop(key)
        for k, v in state.items():
            setattr(self, k, v)

    def __getitem__(self, key) -> Parameter:
        """
        Returns the class or instance parameter like a dictionary dict[key] syntax lookup
        """
        # code change comment -
        # metaclass instance has a param attribute remember, no need to repeat logic of self_.self_or_cls
        # as we create only one instance of Parameters object 
        return self.objects()[key] # if self.owner_inst is None else self.owner_inst.param.objects(False)
  
    def __getattr__(self, attr : str) -> Parameter:
        """
        Extends attribute access to parameter objects.
        """
        try:
            self.__getitem__(attr)
        except KeyError:
            raise AttributeError("'%s.param' object has no attribute %r" %
                                 (self.owner_cls, attr))

    def __dir__(self) -> List[Any]:
        """
        Adds parameters to dir
        """
        return super(ClassParameters, self).__dir__() + list(self)

    def __iter__(self) -> Parameter:
        """
        Iterates over the parameters on this object.
        """
        for p in self.objects():
            yield p

    def __contains__(self, param : Parameter) -> bool:
        return param in list(self) 

    @classmethod
    def _changed(cls, event : Event) -> bool:
        """
        Predicate that determines whether a Event object has actually
        changed such that old != new.
        """
        return not Comparator.is_equal(event.old, event.new)

    def objects(self) -> Dict[str, Parameter]:
        try:
            paramdict = getattr(self.owner_cls, '_%s__params' % self.owner_cls.__name__)
        except AttributeError:
            paramdict = {}
            for class_ in classlist(self.owner_cls):
                for name, val in class_.__dict__.items():
                    if isinstance(val, Parameter):
                        paramdict[name] = val
            # We only want the cache to be visible to the cls on which
            # params() is called, so we mangle the name ourselves at
            # runtime (if we were to mangle it now, it would be
            # _Parameterized.__params for all classes).
            setattr(self.owner_cls, '_%s__params' % self.owner_cls.__name__, paramdict)
        return paramdict

    def keys(self) -> List[str]:
        return self.objects.keys()

    def add_parameter(self, param_name : str, param_obj : Parameter) -> None:
        """
        Add a new Parameter object into this object's class.

        Should result in a Parameter equivalent to one declared
        in the class's source code.
        """
        # Could have just done setattr(cls,param_name,param_obj),
        # which is supported by the metaclass's __setattr__ , but
        # would need to handle the params() cache as well
        # (which is tricky but important for startup speed).
        setattr(self.owner_cls, param_name, param_obj)
        ParameterizedMetaclass._initialize_parameter(cls,param_name,param_obj)
        # delete cached params()
        try:
            delattr(self.owner_cls, '_%s__params'%self.owner_cls.__name__)
        except AttributeError:
            pass

    def get_value_generator(self, name : str):
        pass 

    def get_param_defaults(self):
        """Print the default values of all cls's Parameters."""
        defaults = {}
        for key,val in self.objects():
            defaults[key] = val.default
        return defaults

        
    


class InstanceParameters(ClassParameters):

    def __init__(self, owner_cls : 'ParameterizedMetaclass', 
                    owner_inst : 'Parameterized') -> None:
        super().__init__(owner_cls=owner_cls)
        self.owner_inst = owner_inst

    def _setup_params(self,**params):
        """
        Initialize default and keyword parameter values.

        First, ensures that all Parameters with 'deep_copy=True'
        (typically used for mutable Parameters) are copied directly
        into each object, to ensure that there is an independent copy
        (to avoid surprising aliasing errors).  Then sets each of the
        keyword arguments, warning when any of them are not defined as
        parameters.

        Constant Parameters can be set during calls to this method.
        """
        ## Deepcopy all 'deep_copy=True' parameters
        # (building a set of names first to avoid redundantly
        # instantiating a later-overridden parent class's parameter)
        param_values_to_deep_copy = {}
        for class_ in classlist(self.owner_cls):
            if isinstance(class_, ParameterizedMetaclass):
                for (k, v) in class_.param.objects().items():
                # (avoid replacing name with the default of None)
                    if v.deep_copy and k != "name":
                        param_values_to_deep_copy[k] = v

        for p in param_values_to_deep_copy.values():
            self._deep_copy_param(p)

        ## keyword arg setting
        for name, val in params.items():
            desc, = self.owner_cls.get_param_descriptor(name) # pylint: disable-msg=E1101
            if not desc:
                continue 
                # Its erroneous to set a non-descriptor with a value from init. 
                # we dont know what that value even means 
            setattr(self, name, val)

    def _deep_copy_param(self, param_obj : Parameter, dict_ : Dict = None, key : str = None) -> None:
        # deepcopy param_obj.default into self.__dict__ (or dict_ if supplied)
        # under the parameter's _internal_name (or key if supplied)
        dict_ = dict_ or self.owner_inst.__dict__
        key = key or param_obj._internal_name
        if shared_parameters._share:
            param_key = (str(type(self.owner_inst)), param_obj.name)
            if param_key in shared_parameters._shared_cache:
                new_object = shared_parameters._shared_cache[param_key]
            else:
                new_object = copy.deepcopy(param_obj.default)
                shared_parameters._shared_cache[param_key] = new_object
        else:
            new_object = copy.deepcopy(param_obj.default)
        dict_[key] = new_object

    def _update_deps(self, attribute=None, init : bool = False):
        init_methods = []
        for method, queued, on_init, constant, dynamic in self._depends['watch']:
            # On initialization set up constant watchers; otherwise
            # clean up previous dynamic watchers for the updated attribute
            dynamic = [d for d in dynamic if attribute is None or d.spec.split(".")[0] == attribute]
            if init:
                constant_grouped = defaultdict(list)
                for dep in _resolve_mcs_deps(self.owner_inst, constant, []):
                    constant_grouped[(id(dep.inst), id(dep.cls), dep.what)].append((None, dep))
                for group in constant_grouped.values():
                    self._watch_group(self.owner_inst, method, queued, group)
                m = getattr(self.owner_inst, method)
                if on_init and m not in init_methods:
                    init_methods.append(m)
            elif dynamic:
                for w in self._dynamic_watchers.pop(method, []):
                    w.unwatch(w)
            else:
                continue

            # Resolve dynamic dependencies one-by-one to be able to trace their watchers
            grouped = defaultdict(list)
            for ddep in dynamic:
                for dep in _resolve_mcs_deps(self.owner_inst, [], [ddep]):
                    grouped[(id(dep.inst), id(dep.cls), dep.what)].append((ddep, dep))

            for group in grouped.values():
                watcher = self._watch_group(self.owner_inst, method, queued, group, attribute)
                self._dynamic_watchers[method].append(watcher)
        for m in init_methods:
            m()

    def _resolve_dynamic_deps(self, obj, dynamic_dep, param_dep, attribute):
        """
        If a subobject whose parameters are being depended on changes
        we should only trigger events if the actual parameter values
        of the new object differ from those on the old subobject,
        therefore we accumulate parameters to compare on a subobject
        change event.

        Additionally we need to make sure to notify the parent object
        if a subobject changes so the dependencies can be
        reinitialized so we return a callback which updates the
        dependencies.
        """
        subobj = obj
        subobjs = [obj]
        for subpath in dynamic_dep.spec.split('.')[:-1]:
            subobj = getattr(subobj, subpath.split(':')[0], None)
            subobjs.append(subobj)

        dep_obj = (param_dep.inst or param_dep.cls)
        if dep_obj not in subobjs[:-1]:
            return None, None, param_dep.what

        depth = subobjs.index(dep_obj)
        callback = None
        if depth > 0:
            def callback(*events):
                """
                If a subobject changes, we need to notify the main
                object to update the dependencies.
                """
                obj.param._update_deps(attribute)

        p = '.'.join(dynamic_dep.spec.split(':')[0].split('.')[depth+1:])
        if p == 'param':
            subparams = [sp for sp in list(subobjs[-1].param)]
        else:
            subparams = [p]

        if ':' in dynamic_dep.spec:
            what = dynamic_dep.spec.split(':')[-1]
        else:
            what = param_dep.what

        return subparams, callback, what

    def _watch_group(self_, obj, name, queued, group, attribute=None):
        """
        Sets up a watcher for a group of dependencies. Ensures that
        if the dependency was dynamically generated we check whether
        a subobject change event actually causes a value change and
        that we update the existing watchers, i.e. clean up watchers
        on the old subobject and create watchers on the new subobject.
        """
        dynamic_dep, param_dep = group[0]
        dep_obj = (param_dep.inst or param_dep.cls)
        params = []
        for _, g in group:
            if g.name not in params:
                params.append(g.name)

        if dynamic_dep is None:
            subparams, callback, what = None, None, param_dep.what
        else:
            subparams, callback, what = self_._resolve_dynamic_deps(
                obj, dynamic_dep, param_dep, attribute)

        mcaller = _m_caller(obj, name, what, subparams, callback)
        return dep_obj.param._watch(
            mcaller, params, param_dep.what, queued=queued, precedence=-1)


    def update(self_, *args, **kwargs):
        """
        For the given dictionary or iterable or set of param=value keyword arguments,
        sets the corresponding parameter of this object or class to the given value.
        """
        BATCH_WATCH = self_._BATCH_WATCH
        self_._BATCH_WATCH = True
        self_or_cls = self_.self_or_cls
        if args:
            if len(args) == 1 and not kwargs:
                kwargs = args[0]
            else:
                self_._BATCH_WATCH = False
                raise ValueError("%s.update accepts *either* an iterable or key=value pairs, not both" %
                                 (self_or_cls.name))

        trigger_params = [k for k in kwargs
                          if ((k in self_.self_or_cls.param) and
                              hasattr(self_.self_or_cls.param[k], '_autotrigger_value'))]

        for tp in trigger_params:
            self_.self_or_cls.param[tp]._mode = 'set'

        for (k, v) in kwargs.items():
            if k not in self_or_cls.param:
                self_.self_or_cls.param._BATCH_WATCH = False
                raise ValueError("'%s' is not a parameter of %s" % (k, self_or_cls.name))
            try:
                setattr(self_or_cls, k, v)
            except:
                self_.self_or_cls.param._BATCH_WATCH = False
                raise

        self_.self_or_cls.param._BATCH_WATCH = BATCH_WATCH
        if not BATCH_WATCH:
            self_._batch_call_watchers()

        for tp in trigger_params:
            p = self_.self_or_cls.param[tp]
            p._mode = 'reset'
            setattr(self_or_cls, tp, p._autotrigger_reset_value)
            p._mode = 'set-reset'


    def objects(self, existing : bool = True) -> Dict[str, Parameter]:
        """
        Returns the Parameters of this instance or class

        If instance=True and called on a Parameterized instance it
        will create instance parameters for all Parameters defined on
        the class. To force class parameters to be returned use
        instance=False. Since classes avoid creating instance
        parameters unless necessary you may also request only existing
        instance parameters to be returned by setting
        instance='existing'.
        """
        # We cache the parameters because this method is called often,
        # and parameters are rarely added (and cannot be deleted)
        pdict = super().objects()        
        if existing:
            if getattr(self.owner_inst, 'initialized', False) and self.owner_inst._instance__params:
                return dict(pdict, **self.owner_inst._instance__params)
            return pdict
        else:
            return {k: self.owner_inst.param[k] for k in pdict}
        

    def trigger(self_, *param_names):
        """
        Trigger watchers for the given set of parameter names. Watchers
        will be triggered whether or not the parameter values have
        actually changed. As a special case, the value will actually be
        changed for a Parameter of type Event, setting it to True so
        that it is clear which Event parameter has been triggered.
        """
        trigger_params = [p for p in self_.self_or_cls.param
                          if hasattr(self_.self_or_cls.param[p], '_autotrigger_value')]
        triggers = {p:self_.self_or_cls.param[p]._autotrigger_value
                    for p in trigger_params if p in param_names}

        events = self_.self_or_cls.param._events
        watchers = self_.self_or_cls.param._watchers
        self_.self_or_cls.param._events  = []
        self_.self_or_cls.param._watchers = []
        param_values = self_.values()
        params = {name: param_values[name] for name in param_names}
        self_.self_or_cls.param._TRIGGER = True
        self_.set_param(**dict(params, **triggers))
        self_.self_or_cls.param._TRIGGER = False
        self_.self_or_cls.param._events += events
        self_.self_or_cls.param._watchers += watchers


    def _update_event_type(self_, watcher : Watcher, event : Event, triggered : bool) -> Event:
        """
        Returns an updated Event object with the type field set appropriately.
        """
        if triggered:
            event_type = 'triggered'
        else:
            event_type = 'changed' if watcher.onlychanged else 'set'
        return Event(what=event.what, name=event.name, obj=event.obj, cls=event.cls,
                     old=event.old, new=event.new, type=event_type)

    def _execute_watcher(self, watcher : Watcher, events : List[Event]) -> None:
        if watcher.mode == 'args':
            args, kwargs = events, {}
        else:
            args, kwargs = (), {event.name: event.new for event in events}

        if iscoroutinefunction(watcher.fn):
            if async_executor is None:
                raise RuntimeError("Could not execute %s coroutine function. "
                                   "Please register a asynchronous executor on "
                                   "param.parameterized.async_executor, which "
                                   "schedules the function on an event loop." %
                                   watcher.fn)
            async_executor(partial(watcher.fn, *args, **kwargs))
        else:
            watcher.fn(*args, **kwargs)

    def _call_watcher(self, watcher : Watcher, event : Event):
        """
        Invoke the given watcher appropriately given an Event object.
        """
        if self._TRIGGER:
            pass
        elif watcher.onlychanged and (not self._changed(event)):
            return

        if self._BATCH_WATCH:
            self._events.append(event)
            if not any(watcher is w for w in self._watchers):
                self._watchers.append(watcher)
        else:
            event = self._update_event_type(watcher, event, self._TRIGGER)
            with _batch_call_watchers(self.owner_inst, enable=watcher.queued, run=False):
                self._execute_watcher(watcher, (event,))

    def _batch_call_watchers(self_):
        """
        Batch call a set of watchers based on the parameter value
        settings in kwargs using the queued Event and watcher objects.
        """
        while self_.self_or_cls.param._events:
            event_dict = OrderedDict([((event.name, event.what), event)
                                      for event in self_.self_or_cls.param._events])
            watchers = self_.self_or_cls.param._watchers[:]
            self_.self_or_cls.param._events = []
            self_.self_or_cls.param._watchers = []

            for watcher in sorted(watchers, key=lambda w: w.precedence):
                events = [self_._update_event_type(watcher, event_dict[(name, watcher.what)],
                                                   self_.self_or_cls.param._TRIGGER)
                          for name in watcher.parameter_names
                          if (name, watcher.what) in event_dict]
                with _batch_call_watchers(self_.self_or_cls, enable=watcher.queued, run=False):
                    self_._execute_watcher(watcher, events)

    def set_dynamic_time_fn(self_,time_fn,sublistattr=None):
        """
        Set time_fn for all Dynamic Parameters of this class or
        instance object that are currently being dynamically
        generated.

        Additionally, sets _Dynamic_time_fn=time_fn on this class or
        instance object, so that any future changes to Dynamic
        Parmeters can inherit time_fn (e.g. if a Number is changed
        from a float to a number generator, the number generator will
        inherit time_fn).

        If specified, sublistattr is the name of an attribute of this
        class or instance that contains an iterable collection of
        subobjects on which set_dynamic_time_fn should be called.  If
        the attribute sublistattr is present on any of the subobjects,
        set_dynamic_time_fn() will be called for those, too.
        """
        self_or_cls = self_.self_or_cls
        self_or_cls._Dynamic_time_fn = time_fn

        if isinstance(self_or_cls,type):
            a = (None,self_or_cls)
        else:
            a = (self_or_cls,)

        for n,p in self_or_cls.param.objects('existing').items():
            if hasattr(p, '_value_is_dynamic'):
                if p._value_is_dynamic(*a):
                    g = self_or_cls.param.get_value_generator(n)
                    g._Dynamic_time_fn = time_fn

        if sublistattr:
            try:
                sublist = getattr(self_or_cls,sublistattr)
            except AttributeError:
                sublist = []

            for obj in sublist:
                obj.param.set_dynamic_time_fn(time_fn,sublistattr)

    def serialize_parameters(self_, subset=None, mode='json'):
        self_or_cls = self_.self_or_cls
        if mode not in Parameter._serializers:
            raise ValueError('Mode %r not in available serialization formats %r'
                             % (mode, list(Parameter._serializers.keys())))
        serializer = Parameter._serializers[mode]
        return serializer.serialize_parameters(self_or_cls, subset=subset)

    def serialize_value(self_, pname, mode='json'):
        self_or_cls = self_.self_or_cls
        if mode not in Parameter._serializers:
            raise ValueError('Mode %r not in available serialization formats %r'
                             % (mode, list(Parameter._serializers.keys())))
        serializer = Parameter._serializers[mode]
        return serializer.serialize_parameter_value(self_or_cls, pname)

    def deserialize_parameters(self_, serialization, subset=None, mode='json'):
        self_or_cls = self_.self_or_cls
        serializer = Parameter._serializers[mode]
        return serializer.deserialize_parameters(self_or_cls, serialization, subset=subset)

    def deserialize_value(self_, pname, value, mode='json'):
        self_or_cls = self_.self_or_cls
        if mode not in Parameter._serializers:
            raise ValueError('Mode %r not in available serialization formats %r'
                             % (mode, list(Parameter._serializers.keys())))
        serializer = Parameter._serializers[mode]
        return serializer.deserialize_parameter_value(self_or_cls, pname, value)

    def schema(self_, safe=False, subset=None, mode='json'):
        """
        Returns a schema for the parameters on this Parameterized object.
        """
        self_or_cls = self_.self_or_cls
        if mode not in Parameter._serializers:
            raise ValueError('Mode %r not in available serialization formats %r'
                             % (mode, list(Parameter._serializers.keys())))
        serializer = Parameter._serializers[mode]
        return serializer.schema(self_or_cls, safe=safe, subset=subset)

    # PARAM2_DEPRECATION: Could be removed post param 2.0; same as values() but returns list, not dict
    def get_param_values(self_, onlychanged=False):
        """
        (Deprecated; use .values() instead.)

        Return a list of name,value pairs for all Parameters of this
        object.

        When called on an instance with onlychanged set to True, will
        only return values that are not equal to the default value
        (onlychanged has no effect when called on a class).
        """
        self_or_cls = self_.self_or_cls
        vals = []
        for name, val in self_or_cls.param.objects('existing').items():
            value = self_or_cls.param.get_value_generator(name)
            if not onlychanged or not all_equal(value, val.default):
                vals.append((name, value))

        vals.sort(key=itemgetter(0))
        return vals

    def values(self_, onlychanged=False):
        """
        Return a dictionary of name,value pairs for the Parameters of this
        object.

        When called on an instance with onlychanged set to True, will
        only return values that are not equal to the default value
        (onlychanged has no effect when called on a class).
        """
        # Defined in terms of get_param_values() to avoid ordering
        # issues in python2, but can be inverted if get_param_values
        # is removed when python2 support is dropped
        return dict(self_.get_param_values(onlychanged))

    def force_new_dynamic_value(self_, name): # pylint: disable-msg=E0213
        """
        Force a new value to be generated for the dynamic attribute
        name, and return it.

        If name is not dynamic, its current value is returned
        (i.e. equivalent to getattr(name).
        """
        cls_or_slf = self_.self_or_cls
        param_obj = cls_or_slf.param.objects('existing').get(name)

        if not param_obj:
            return getattr(cls_or_slf, name)

        cls, slf = None, None
        if isinstance(cls_or_slf,type):
            cls = cls_or_slf
        else:
            slf = cls_or_slf

        if not hasattr(param_obj,'_force'):
            return param_obj.__get__(slf, cls)
        else:
            return param_obj._force(slf, cls)

    def get_value_generator(self, name : str) -> Any: # pylint: disable-msg=E0213
        """
        Return the value or value-generating object of the named
        attribute.

        For most parameters, this is simply the parameter's value
        (i.e. the same as getattr()), but Dynamic parameters have
        their value-generating object returned.
        """
        param_obj = self.objects().get(name)

        if not param_obj:
            value = getattr(self.owner_inst, name)

        # CompositeParameter detected by being a Parameter and having 'attribs'
        elif hasattr(param_obj, 'attribs'):
            value = [self.get_value_generator(a) for a in param_obj.attribs]

        # not a Dynamic Parameter
        elif not hasattr(param_obj,'_value_is_dynamic'):
            value = getattr(self.owner_inst, name)

        # Dynamic Parameter...
        else:
            internal_name = "_%s_param_value"%name
            if hasattr(self.owner_inst, internal_name):
                # dealing with object and it's been set on this object
                value = getattr(self.owner_inst, internal_name)
            else:
                raise ValueError("")
        return value

    def inspect_value(self, name : str) -> Any: # pylint: disable-msg=E0213
        """
        Return the current value of the named attribute without modifying it.

        Same as getattr() except for Dynamic parameters, which have their
        last generated value returned.
        """
        param_obj = self.objects().get(name)

        if not param_obj:
            value = getattr(self.owner_inst, name)
        elif hasattr(param_obj, 'attribs'):
            value = [self.inspect_value(a) for a in param_obj.attribs]
        elif not hasattr(param_obj, '_inspect'):
            value = getattr(self.owner_inst, name)
        else:
            if isinstance(, type):
                value = param_obj._inspect(None, cls_or_slf)
            else:
                value = param_obj._inspect(cls_or_slf, None)
        return value

    def method_dependencies(self_, name, intermediate=False):
        """
        Given the name of a method, returns a PInfo object for each dependency
        of this method. See help(PInfo) for the contents of these objects.

        By default intermediate dependencies on sub-objects are not
        returned as these are primarily useful for internal use to
        determine when a sub-object dependency has to be updated.
        """
        method = getattr(self_.self_or_cls, name)
        minfo = MInfo(cls=self_.cls, inst=self_.self, name=name,
                      method=method)
        deps, dynamic = _params_depended_on(
            minfo, dynamic=False, intermediate=intermediate)
        if self_.self is None:
            return deps
        return _resolve_mcs_deps(
            self_.self, deps, dynamic, intermediate=intermediate)

    # PARAM2_DEPRECATION: Backwards compatibilitity for param<1.12
    params_depended_on = method_dependencies

    

    def _spec_to_obj(self_, spec, dynamic=True, intermediate=True):
        """
        Resolves a dependency specification into lists of explicit
        parameter dependencies and dynamic dependencies.

        Dynamic dependencies are specifications to be resolved when
        the sub-object whose parameters are being depended on is
        defined.

        During class creation dynamic=False which means sub-object
        dependencies are not resolved. At instance creation and
        whenever a sub-object is set on an object this method will be
        invoked to determine whether the dependency is available.

        For sub-object dependencies we also return dependencies for
        every part of the path, e.g. for a dependency specification
        like "a.b.c" we return dependencies for sub-object "a" and the
        sub-sub-object "b" in addition to the dependency on the actual
        parameter "c" on object "b". This is to ensure that if a
        sub-object is swapped out we are notified and can update the
        dynamic dependency to the new object. Even if a sub-object
        dependency can only partially resolved, e.g. if object "a"
        does not yet have a sub-object "b" we must watch for changes
        to "b" on sub-object "a" in case such a subobject is put in "b".
        """
        if isinstance(spec, Parameter):
            inst = spec.owner if isinstance(spec.owner, Parameterized) else None
            cls = spec.owner if inst is None else type(inst)
            info = PInfo(inst=inst, cls=cls, name=spec.name,
                         pobj=spec, what='value')
            return [] if intermediate == 'only' else [info], []

        obj, attr, what = _parse_dependency_spec(spec)
        if obj is None:
            src = self_.self_or_cls
        elif not dynamic:
            return [], [DInfo(spec=spec)]
        else:
            src = _getattrr(self_.self_or_cls, obj[1::], None)
            if src is None:
                path = obj[1:].split('.')
                deps = []
                # Attempt to partially resolve subobject path to ensure
                # that if a subobject is later updated making the full
                # subobject path available we have to be notified and
                # set up watchers
                if len(path) >= 1 and intermediate:
                    sub_src = None
                    subpath = path
                    while sub_src is None and subpath:
                        subpath = subpath[:-1]
                        sub_src = _getattrr(self_.self_or_cls, '.'.join(subpath), None)
                    if subpath:
                        subdeps, _ = self_._spec_to_obj(
                            '.'.join(path[:len(subpath)+1]), dynamic, intermediate)
                        deps += subdeps
                return deps, [] if intermediate == 'only' else [DInfo(spec=spec)]

        cls, inst = (src, None) if isinstance(src, type) else (type(src), src)
        if attr == 'param':
            deps, dynamic_deps = self_._spec_to_obj(obj[1:], dynamic, intermediate)
            for p in src.param:
                param_deps, param_dynamic_deps = src.param._spec_to_obj(p, dynamic, intermediate)
                deps += param_deps
                dynamic_deps += param_dynamic_deps
            return deps, dynamic_deps
        elif attr in src.param:
            info = PInfo(inst=inst, cls=cls, name=attr,
                         pobj=src.param[attr], what=what)
        elif hasattr(src, attr):
            info = MInfo(inst=inst, cls=cls, name=attr,
                         method=getattr(src, attr))
        elif getattr(src, "abstract", None):
            return [], [] if intermediate == 'only' else [DInfo(spec=spec)]
        else:
            raise AttributeError("Attribute %r could not be resolved on %s."
                                 % (attr, src))

        if obj is None or not intermediate:
            return [info], []
        deps, dynamic_deps = self_._spec_to_obj(obj[1:], dynamic, intermediate)
        if intermediate != 'only':
            deps.append(info)
        return deps, dynamic_deps

    def _register_watcher(self, action, watcher : Watcher, what : str = 'value'):
        parameter_names = watcher.parameter_names
        for parameter_name in parameter_names:
            if parameter_name not in self:
                raise ValueError("%s parameter was not found in list of "
                                 "parameters of class %s" %
                                 (parameter_name, self_.cls.__name__))

            if self_.self is not None and what == "value":
                watchers = self_.self._param_watchers
                if parameter_name not in watchers:
                    watchers[parameter_name] = {}
                if what not in watchers[parameter_name]:
                    watchers[parameter_name][what] = []
                getattr(watchers[parameter_name][what], action)(watcher)
            else:
                watchers = self_[parameter_name].watchers
                if what not in watchers:
                    watchers[what] = []
                getattr(watchers[what], action)(watcher)

    def watch(self, fn : Callable, parameter_names : Union[List[str], Tuple[str], str], what : str = 'value', 
                onlychanged : bool = True, queued : bool = False, precedence : float = 0 ):
        """
        Register the given callback function `fn` to be invoked for
        events on the indicated parameters.

        `what`: What to watch on each parameter; either the value (by
        default) or else the indicated slot (e.g. 'constant').

        `onlychanged`: By default, only invokes the function when the
        watched item changes, but if `onlychanged=False` also invokes
        it when the `what` item is set to its current value again.

        `queued`: By default, additional watcher events generated
        inside the callback fn are dispatched immediately, effectively
        doing depth-first processing of Watcher events. However, in
        certain scenarios, it is helpful to wait to dispatch such
        downstream events until all events that triggered this watcher
        have been processed. In such cases setting `queued=True` on
        this Watcher will queue up new downstream events generated
        during `fn` until `fn` completes and all other watchers
        invoked by that same event have finished executing),
        effectively doing breadth-first processing of Watcher events.

        `precedence`: Declares a precedence level for the Watcher that
        determines the priority with which the callback is executed.
        Lower precedence levels are executed earlier. Negative
        precedences are reserved for internal Watchers, i.e. those
        set up by param.depends.

        When the `fn` is called, it will be provided the relevant
        Event objects as positional arguments, which allows it to
        determine which of the possible triggering events occurred.

        Returns a Watcher object.

        See help(Watcher) and help(Event) for the contents of those objects.
        """
        if precedence < 0:
            raise ValueError("User-defined watch callbacks must declare "
                             "a positive precedence. Negative precedences "
                             "are reserved for internal Watchers.")
    #     return self_._watch(fn, parameter_names, what, onlychanged, queued, precedence)

    # def _watch(self_, fn, parameter_names, what='value', onlychanged=True, queued=False, precedence=-1):
        parameter_names = tuple(parameter_names) if isinstance(parameter_names, list) else (parameter_names,)
        watcher = Watcher( inst=self.owner_inst, cls=self.owner_cls, fn=fn, mode='args',
                          onlychanged=onlychanged, parameter_names=parameter_names,
                          what=what, queued=queued, precedence=precedence)
        self._register_watcher('append', watcher, what)
        return watcher

    def unwatch(self, watcher : Watcher) -> None:
        """
        Remove the given Watcher object (from `watch` or `watch_values`) from this object's list.
        """
        self._register_watcher('remove', watcher, what=watcher.what)
        
    def watch_values(self_, fn :  Callable, parameter_names : Union[List[str], Tuple[str], str], 
                        onlychanged : bool = True, queued : bool=False, precedence : float = 0):
        """
        Easier-to-use version of `watch` specific to watching for changes in parameter values.

        Only allows `what` to be 'value', and invokes the callback `fn` using keyword
        arguments <param_name>=<new_value> rather than with a list of Event objects.
        """
        if precedence < 0:
            raise ValueError("User-defined watch callbacks must declare "
                             "a positive precedence. Negative precedences "
                             "are reserved for internal Watchers.")
        if isinstance(parameter_names, list):
            parameter_names = tuple(parameter_names)
        else:
            parameter_names = (parameter_names,)
        watcher = Watcher(inst=self_.self, cls=self_.cls, fn=fn,
                          mode='kwargs', onlychanged=onlychanged,
                          parameter_names=parameter_names, what='value',
                          queued=queued, precedence=precedence)
        self_._register_watcher('append', watcher, 'value')
        return watcher

    def outputs(self):
        """
        Returns a mapping between any declared outputs and a tuple
        of the declared Parameter type, the output method, and the
        index into the output if multiple outputs are returned.
        """
        outputs = {}
        for cls in classlist(self.owner_cls):
            for name in dir(cls):
                method = getattr(self.owner_inst, name)
                dinfo = getattr(method, '_dinfo', {})
                if 'outputs' not in dinfo:
                    continue
                for override, otype, idx in dinfo['outputs']:
                    if override is not None:
                        name = override
                    outputs[name] = (otype, method, idx)
        return outputs
    

    # Instance methods

    # PARAM2_DEPRECATION: Backwards compatibilitity for param<1.12
    def defaults(self_):
        """
        Return {parameter_name:parameter.default} for all non-constant
        Parameters.

        Note that a Parameter for which deep_copy==True has its default
        deepcopied.
        """
        self = self_.self
        d = {}
        for param_name, param in self.param.objects('existing').items():
            if param.constant:
                pass
            if param.deep_copy:
                self.param._deep_copy_param(param, dict_=d, key=param_name)
            d[param_name] = param.default
        return d

    # PARAM2_DEPRECATION: Backwards compatibilitity for param<1.12
    def print_param_values(self_):
        """Print the values of all this object's Parameters."""
        self = self_.self
        for name, val in self.param.values().items():
            print('%s.%s = %s' % (self.name,name,val))

    def pprint(self_, imports=None, prefix=" ", unknown_value='<?>',
               qualify=False, separator=""):
        """See Parameterized.pprint"""
        self = self_.self
        return self._pprint(imports, prefix, unknown_value, qualify, separator)



class ParameterizedMetaclass(type):
    """
    The metaclass of Parameterized (and all its descendents).

    The metaclass overrides type.__setattr__ to allow us to set
    Parameter values on classes without overwriting the attribute
    descriptor.  That is, for a Parameterized class of type X with a
    Parameter y, the user can type X.y=3, which sets the default value
    of Parameter y to be 3, rather than overwriting y with the
    constant value 3 (and thereby losing all other info about that
    Parameter, such as the doc string, bounds, etc.).

    The __init__ method is used when defining a Parameterized class,
    usually when the module where that class is located is imported
    for the first time.  That is, the __init__ in this metaclass
    initializes the *class* object, while the __init__ method defined
    in each Parameterized class is called for each new instance of
    that class.

    Additionally, a class can declare itself abstract by having an
    attribute __abstract set to True. The 'abstract' attribute can be
    used to find out if a class is abstract or not.
    """
    def __init__(mcs, name : str, bases : Tuple[Union[type, object]], dict_ : dict) -> None:
        """
        Initialize the class object (not an instance of the class, but
        the class itself).
        """
        type.__init__(mcs, name, bases, dict_)

        # Give Parameterized classes a useful 'name' attribute.
        mcs.name = name

        mcs._parameters_state = {
            "BATCH_WATCH": False, # If true, Event and watcher objects are queued.
            "TRIGGER": False,
            "events": [], # Queue of batched events
            "watchers": [] # Queue of batched watchers
        }
        mcs._param = ClassParameters(mcs)      
        mcs._param_container = mcs._param 
        # we try to use _param_container from now on and the above is only for 
        # until bugs are removed

        # All objects (with their names) of type Parameter that are
        # defined in this class
        parameters = [(n, o) for (n, o) in dict_.items()
                      if isinstance(o, Parameter)]

        mcs.param._parameters = dict(parameters)

        for param_name, param in parameters:
            mcs._initialize_parameter(param_name, param)

        # retrieve depends info from methods and store more conveniently
        dependers = [(n, m, m._dinfo) for (n, m) in dict_.items()
                     if hasattr(m, '_dinfo')]

        # Resolve dependencies of current class
        _watch = []
        for name, method, dinfo in dependers:
            watch = dinfo.get('watch', False)
            on_init = dinfo.get('on_init', False)
            if not watch:
                continue
            minfo = MInfo(cls=mcs, inst=None, name=name,
                          method=method)
            deps, dynamic_deps = _params_depended_on(minfo, dynamic=False)
            _watch.append((name, watch == 'queued', on_init, deps, dynamic_deps))

        # Resolve dependencies in class hierarchy
        _inherited = []
        for cls in classlist(mcs)[:-1][::-1]:
            if not hasattr(cls, '_param'):
                continue
            for dep in cls.param._depends['watch']:
                method = getattr(mcs, dep[0], None)
                dinfo = getattr(method, '_dinfo', {'watch': False})
                if (not any(dep[0] == w[0] for w in _watch+_inherited)
                    and dinfo.get('watch')):
                    _inherited.append(dep)

        mcs.param._depends = {'watch': _inherited+_watch}

        if docstring_signature:
            mcs.__update_docstring_signature()

    def __update_docstring_signature(mcs) -> None:
        """
        Autogenerate a keyword signature in the class docstring for
        all available parameters. This is particularly useful in the
        IPython Notebook as IPython will parse this signature to allow
        tab-completion of keywords.

        max_repr_len: Maximum length (in characters) of value reprs.
        """
        processed_kws, keyword_groups = set(), []
        for cls in reversed(mcs.mro()):
            keyword_group = []
            for (k,v) in sorted(cls.__dict__.items()):
                if isinstance(v, Parameter) and k not in processed_kws:
                    param_type = v.__class__.__name__
                    keyword_group.append("%s=%s" % (k, param_type))
                    processed_kws.add(k)
            keyword_groups.append(keyword_group)

        keywords = [el for grp in reversed(keyword_groups) for el in grp]
        class_docstr = "\n"+mcs.__doc__ if mcs.__doc__ else ''
        signature = "params(%s)" % (", ".join(keywords))
        description = param_pager(mcs) if (docstring_describe_params and param_pager) else ''
        mcs.__doc__ = signature + class_docstr + '\n' + description


    def _initialize_parameter(mcs, param_name : str, param : Parameter) -> None:
        setattr(param, 'owner', mcs)

    # Should use the official Python 2.6+ abstract base classes; see
    # https://github.com/holoviz/param/issues/84
    @property
    def abstract(mcs) -> bool:
        """
        Return True if the class has an attribute __abstract set to True.
        Subclasses will return False unless they themselves have
        __abstract set to true.  This mechanism allows a class to
        declare itself to be abstract (e.g. to avoid it being offered
        as an option in a GUI), without the "abstract" property being
        inherited by its subclasses (at least one of which is
        presumably not abstract).
        """
        # Can't just do ".__abstract", because that is mangled to
        # _ParameterizedMetaclass__abstract before running, but
        # the actual class object will have an attribute
        # _ClassName__abstract.  So, we have to un-mangle it ourselves at
        # runtime. Mangling follows description in
        # https://docs.python.org/2/tutorial/classes.html#private-variables-and-class-local-references
        try:
            return getattr(mcs,'_%s__abstract'%mcs.__name__.lstrip("_"))
        except AttributeError:
            return False

    @property
    def param(mcs) -> ClassParameters:
        return mcs._param_container

    def __setattr__(mcs, attribute_name : str, value : Any) -> None:
        """
        Implements 'self.attribute_name=value' in a way that also supports Parameters.

        If there is already a descriptor named attribute_name, and
        that descriptor is a Parameter, and the new value is *not* a
        Parameter, then call that Parameter's __set__ method with the
        specified value.

        In all other cases set the attribute normally (i.e. overwrite
        the descriptor).  If the new value is a Parameter, once it has
        been set we make sure that the value is inherited from
        Parameterized superclasses as described in __param_inheritance().
        """
        # Find out if there's a Parameter called attribute_name as a
        # class attribute of this class - if not, parameter is None.
        parameter, = mcs.get_param_descriptor(attribute_name)

        # checking isinstance(value, Parameter) will not work for ClassSelector 
        # and besides value is anyway validated. On the downside, this does not allow
        # altering of parameters instances if class already of the parameter with attribute_name
        if parameter: # and not isinstance(value, Parameter): 
            # if owning_class != mcs:
            #     parameter = copy.copy(parameter)
            #     parameter.owner = mcs
            #     type.__setattr__(mcs, attribute_name, parameter)
            mcs.__dict__[attribute_name].__set__(mcs, value)
            # set with None should not supported as with mcs it supports 
            # class attributes which can be validated
        else:
            type.__setattr__(mcs, attribute_name, value)
            if isinstance(value, Parameter):
                mcs._initialize_parameter(attribute_name, value)
            

    def get_param_descriptor(mcs, param_name : str) -> Union[Tuple[Parameter, 'Parameterized'], Tuple[None, None]]:
        """
        Goes up the class hierarchy (starting from the current class)
        looking for a Parameter class attribute param_name. As soon as
        one is found as a class attribute, that Parameter is returned
        along with the class in which it is declared.
        """
        for c in classlist(mcs)[::-1]:
            attribute = c.__dict__.get(param_name)
            if isinstance(attribute, Parameter):
                return attribute, c
        return None, None




# Whether script_repr should avoid reporting the values of parameters
# that are just inheriting their values from the class defaults.
# Because deepcopying creates a new object, cannot detect such
# inheritance when deep_copy = True, so such values will be printed
# even if they are just being copied from the default.
script_repr_suppress_defaults=True


def script_repr(val, imports=None, prefix="\n    ", settings=[],
        qualify=True, unknown_value=None, separator="\n",
        show_imports=True):
    """
    Variant of pprint() designed for generating a (nearly) runnable script.

    The output of script_repr(parameterized_obj) is meant to be a
    string suitable for running using `python file.py`. Not every
    object is guaranteed to have a runnable script_repr
    representation, but it is meant to be a good starting point for
    generating a Python script that (after minor edits) can be
    evaluated to get a newly initialized object similar to the one
    provided.

    The new object will only have the same parameter state, not the
    same internal (attribute) state; the script_repr captures only
    the state of the Parameters of that object and not any other
    attributes it may have.

    If show_imports is True (default), includes import statements
    for each of the modules required for the objects being
    instantiated. This list may not be complete, as it typically
    includes only the imports needed for the Parameterized object
    itself, not for values that may have been supplied to Parameters.

    Apart from show_imports, accepts the same arguments as pprint(),
    so see pprint() for explanations of the arguments accepted. The
    default values of each of these arguments differ from pprint() in
    ways that are more suitable for saving as a separate script than
    for e.g. pretty-printing at the Python prompt.
    """

    if imports is None:
        imports = []

    rep = pprint(val, imports, prefix, settings, unknown_value,
                 qualify, separator)

    imports = list(set(imports))
    imports_str = ("\n".join(imports) + "\n\n") if show_imports else ""

    return imports_str + rep


# PARAM2_DEPRECATION: Remove entirely unused settings argument
def pprint(val,imports=None, prefix="\n    ", settings=[],
           unknown_value='<?>', qualify=False, separator=''):
    """
    Pretty printed representation of a parameterized
    object that may be evaluated with eval.

    Similar to repr except introspection of the constructor (__init__)
    ensures a valid and succinct representation is generated.

    Only parameters are represented (whether specified as standard,
    positional, or keyword arguments). Parameters specified as
    positional arguments are always shown, followed by modified
    parameters specified as keyword arguments, sorted by precedence.

    unknown_value determines what to do where a representation cannot be
    generated for something required to recreate the object. Such things
    include non-parameter positional and keyword arguments, and certain
    values of parameters (e.g. some random state objects).

    Supplying an unknown_value of None causes unrepresentable things
    to be silently ignored. If unknown_value is a string, that
    string will appear in place of any unrepresentable things. If
    unknown_value is False, an Exception will be raised if an
    unrepresentable value is encountered.

    If supplied, imports should be a list, and it will be populated
    with the set of imports required for the object and all of its
    parameter values.

    If qualify is True, the class's path will be included (e.g. "a.b.C()"),
    otherwise only the class will appear ("C()").

    Parameters will be separated by a comma only by default, but the
    separator parameter allows an additional separator to be supplied
    (e.g. a newline could be supplied to have each Parameter appear on a
    separate line).

    Instances of types that require special handling can use the
    script_repr_reg dictionary. Using the type as a key, add a
    function that returns a suitable representation of instances of
    that type, and adds the required import statement. The repr of a
    parameter can be suppressed by returning None from the appropriate
    hook in script_repr_reg.
    """

    if imports is None:
        imports = []

    if isinstance(val,type):
        rep = type_script_repr(val,imports,prefix,settings)

    elif type(val) in script_repr_reg:
        rep = script_repr_reg[type(val)](val,imports,prefix,settings)

    elif hasattr(val,'_pprint'):
        rep=val._pprint(imports=imports, prefix=prefix+"    ",
                        qualify=qualify, unknown_value=unknown_value,
                        separator=separator)
    else:
        rep=repr(val)

    return rep


# Registry for special handling for certain types in script_repr and pprint
script_repr_reg = {}


# currently only handles list and tuple
def container_script_repr(container,imports,prefix,settings):
    result=[]
    for i in container:
        result.append(pprint(i,imports,prefix,settings))

    ## (hack to get container brackets)
    if isinstance(container,list):
        d1,d2='[',']'
    elif isinstance(container,tuple):
        d1,d2='(',')'
    else:
        raise NotImplementedError
    rep=d1+','.join(result)+d2

    # no imports to add for built-in types

    return rep


def empty_script_repr(*args): # pyflakes:ignore (unused arguments):
    return None

try:
    # Suppress scriptrepr for objects not yet having a useful string representation
    import numpy
    script_repr_reg[random.Random] = empty_script_repr
    script_repr_reg[numpy.random.RandomState] = empty_script_repr

except ImportError:
    pass # Support added only if those libraries are available


def function_script_repr(fn,imports,prefix,settings):
    name = fn.__name__
    module = fn.__module__
    imports.append('import %s'%module)
    return module+'.'+name

def type_script_repr(type_,imports,prefix,settings):
    module = type_.__module__
    if module!='__builtin__':
        imports.append('import %s'%module)
    return module+'.'+type_.__name__

script_repr_reg[list]=container_script_repr
script_repr_reg[tuple]=container_script_repr
script_repr_reg[FunctionType]=function_script_repr


#: If not None, the value of this Parameter will be called (using '()')
#: before every call to __db_print, and is expected to evaluate to a
#: string that is suitable for prefixing messages and warnings (such
#: as some indicator of the global state).
dbprint_prefix=None


# Copy of Python 3.2 reprlib's recursive_repr but allowing extra arguments
if sys.version_info.major >= 3:
    from threading import get_ident
    def recursive_repr(fillvalue='...'):
        'Decorator to make a repr function return fillvalue for a recursive call'

        def decorating_function(user_function):
            repr_running = set()

            def wrapper(self, *args, **kwargs):
                key = id(self), get_ident()
                if key in repr_running:
                    return fillvalue
                repr_running.add(key)
                try:
                    result = user_function(self, *args, **kwargs)
                finally:
                    repr_running.discard(key)
                return result
            return wrapper

        return decorating_function
else:
    def recursive_repr(fillvalue='...'):
        def decorating_function(user_function):
            return user_function
        return decorating_function


class Parameterized(metaclass=ParameterizedMetaclass):
    """
    Base class for named objects that support Parameters and message
    formatting.

    Automatic object naming: Every Parameterized instance has a name
    parameter.  If the user doesn't designate a name=<str> argument
    when constructing the object, the object will be given a name
    consisting of its class name followed by a unique 5-digit number.

    Automatic parameter setting: The Parameterized __init__ method
    will automatically read the list of keyword parameters.  If any
    keyword matches the name of a Parameter (see Parameter class)
    defined in the object's class or any of its superclasses, that
    parameter in the instance will get the value given as a keyword
    argument.  For example:

      class Foo(Parameterized):
         xx = Parameter(default=1)

      foo = Foo(xx=20)

    in this case foo.xx gets the value 20.

    When initializing a Parameterized instance ('foo' in the example
    above), the values of parameters can be supplied as keyword
    arguments to the constructor (using parametername=parametervalue);
    these values will override the class default values for this one
    instance.

    If no 'name' parameter is supplied, self.name defaults to the
    object's class name with a unique number appended to it.

    Message formatting: Each Parameterized instance has several
    methods for optionally printing output. This functionality is
    based on the standard Python 'logging' module; using the methods
    provided here, wraps calls to the 'logging' module's root logger
    and prepends each message with information about the instance
    from which the call was made. For more information on how to set
    the global logging level and change the default message prefix,
    see documentation for the 'logging' module.
    """

    name = String(default=None, constant=True, doc="""
        String identifier for this object.""")

    def __init__(self, **params):
        global object_count

        # Flag that can be tested to see if e.g. constant Parameters
        # can still be set
        self.initialized = False
        self._parameters_state = {
            "BATCH_WATCH": False, # If true, Event and watcher objects are queued.
            "TRIGGER": False,
            "events": [], # Queue of batched events
            "watchers": [] # Queue of batched watchers
        }
        self._instance__params = {}
        self._param_watchers = {}
        self._dynamic_watchers = defaultdict(list)
        self._param_container = InstanceParameters(self.__class__, self=self)

        self.param._generate_name()
        self.param._setup_params(**params)
        object_count += 1

        self.param._update_deps(init=True)

        self.initialized = True

    @property
    def param(self) -> InstanceParameters:
        return self._param_container
    
    # 'Special' methods

    def __getstate__(self):
        """
        Save the object's state: return a dictionary that is a shallow
        copy of the object's __dict__ and that also includes the
        object's __slots__ (if it has any).
        
        Note that Parameterized object pickling assumes that
        attributes to be saved are only in __dict__ or __slots__
        (the standard Python places to store attributes, so that's a
        reasonable assumption). (Additionally, class attributes that
        are Parameters are also handled, even when they haven't been
        instantiated - see PickleableClassAttributes.)
        """
        state = self.__dict__.copy()
        for slot in get_occupied_slots(self):
            state[slot] = getattr(self,slot)
        return state

    def __setstate__(self, state):
        """
        Restore objects from the state dictionary to this object.

        During this process the object is considered uninitialized.
        """
        self.initialized=False

        # When making a copy the internal watchers have to be
        # recreated and point to the new instance
        if '_param_watchers' in state:
            param_watchers = state['_param_watchers']
            for p, attrs in param_watchers.items():
                for attr, watchers in attrs.items():
                    new_watchers = []
                    for watcher in watchers:
                        watcher_args = list(watcher)
                        if watcher.inst is not None:
                            watcher_args[0] = self
                        fn = watcher.fn
                        if hasattr(fn, '_watcher_name'):
                            watcher_args[2] = _m_caller(self, fn._watcher_name)
                        elif get_method_owner(fn) is watcher.inst:
                            watcher_args[2] = getattr(self, fn.__name__)
                        new_watchers.append(Watcher(*watcher_args))
                    param_watchers[p][attr] = new_watchers

        if '_instance__params' not in state:
            state['_instance__params'] = {}
        if '_param_watchers' not in state:
            state['_param_watchers'] = {}
        state.pop('param', None)

        for name,value in state.items():
            setattr(self,name,value)
        self.initialized=True

    @recursive_repr()
    def __repr__(self):
        """
        Provide a nearly valid Python representation that could be used to recreate
        the item with its parameters, if executed in the appropriate environment.

        Returns 'classname(parameter1=x,parameter2=y,...)', listing
        all the parameters of this object.
        """
        try:
            settings = ['%s=%s' % (name, repr(val))
                        # PARAM2_DEPRECATION: Update to self.param.values.items()
                        # (once python2 support is dropped)
                        for name, val in self.param.get_param_values()]
        except RuntimeError: # Handle recursion in parameter depth
            settings = []
        return self.__class__.__name__ + "(" + ", ".join(settings) + ")"

    def __str__(self):
        """Return a short representation of the name and class of this object."""
        return "<%s %s>" % (self.__class__.__name__, self.name)


    # PARAM2_DEPRECATION: Remove this compatibility alias for param 2.0 and later; use self.param.pprint instead
    def script_repr(self,imports=[],prefix="    "):
        """
        Deprecated variant of __repr__ designed for generating a runnable script.
        """
        return self.pprint(imports,prefix, unknown_value=None, qualify=True,
                           separator="\n")

    @recursive_repr()
    def _pprint(self, imports=None, prefix=" ", unknown_value='<?>',
               qualify=False, separator=""):
        """
        (Experimental) Pretty printed representation that may be
        evaluated with eval. See pprint() function for more details.
        """
        if imports is None:
            imports = [] # would have been simpler to use a set from the start
        imports[:] = list(set(imports))

        # Generate import statement
        mod = self.__module__
        bits = mod.split('.')
        imports.append("import %s"%mod)
        imports.append("import %s"%bits[0])

        changed_params = self.param.values(onlychanged=script_repr_suppress_defaults)
        values = self.param.values()
        spec = getfullargspec(self.__init__)
        args = spec.args[1:] if spec.args[0] == 'self' else spec.args

        if spec.defaults is not None:
            posargs = spec.args[:-len(spec.defaults)]
            kwargs = dict(zip(spec.args[-len(spec.defaults):], spec.defaults))
        else:
            posargs, kwargs = args, []

        parameters = self.param.objects('existing')
        ordering = sorted(
            sorted(changed_params), # alphanumeric tie-breaker
            key=lambda k: (- float('inf')  # No precedence is lowest possible precendence
                           if parameters[k].precedence is None else
                           parameters[k].precedence))

        arglist, keywords, processed = [], [], []
        for k in args + ordering:
            if k in processed: continue

            # Suppresses automatically generated names.
            if k == 'name' and (values[k] is not None
                                and re.match('^'+self.__class__.__name__+'[0-9]+$', values[k])):
                continue

            value = pprint(values[k], imports, prefix=prefix,settings=[],
                           unknown_value=unknown_value,
                           qualify=qualify) if k in values else None

            if value is None:
                if unknown_value is False:
                    raise Exception("%s: unknown value of %r" % (self.name,k))
                elif unknown_value is None:
                    # i.e. suppress repr
                    continue
                else:
                    value = unknown_value

            # Explicit kwarg (unchanged, known value)
            if (k in kwargs) and (k in values) and kwargs[k] == values[k]: continue

            if k in posargs:
                # value will be unknown_value unless k is a parameter
                arglist.append(value)
            elif (k in kwargs or
                  (hasattr(spec, 'varkw') and (spec.varkw is not None)) or
                  (hasattr(spec, 'keywords') and (spec.keywords is not None))):
                # Explicit modified keywords or parameters in
                # precendence order (if **kwargs present)
                keywords.append('%s=%s' % (k, value))

            processed.append(k)

        qualifier = mod + '.'  if qualify else ''
        arguments = arglist + keywords + (['**%s' % spec.varargs] if spec.varargs else [])
        return qualifier + '%s(%s)' % (self.__class__.__name__,  (','+separator+prefix).join(arguments))

    # PARAM2_DEPRECATION: Backwards compatibilitity for param<1.12
    pprint = _pprint

    # Note that there's no state_push method on the class, so
    # dynamic parameters set on a class can't have state saved. This
    # is because, to do this, state_push() would need to be a
    # @bothmethod, but that complicates inheritance in cases where we
    # already have a state_push() method.
    # (isinstance(g,Parameterized) below is used to exclude classes.)

    def state_push(self):
        """
        Save this instance's state.

        For Parameterized instances, this includes the state of
        dynamically generated values.

        Subclasses that maintain short-term state should additionally
        save and restore that state using state_push() and
        state_pop().

        Generally, this method is used by operations that need to test
        something without permanently altering the objects' state.
        """
        for pname, p in self.param.objects('existing').items():
            g = self.param.get_value_generator(pname)
            if hasattr(g,'_Dynamic_last'):
                g._saved_Dynamic_last.append(g._Dynamic_last)
                g._saved_Dynamic_time.append(g._Dynamic_time)
                # CB: not storing the time_fn: assuming that doesn't
                # change.
            elif hasattr(g,'state_push') and isinstance(g,Parameterized):
                g.state_push()

    def state_pop(self):
        """
        Restore the most recently saved state.

        See state_push() for more details.
        """
        for pname, p in self.param.objects('existing').items():
            g = self.param.get_value_generator(pname)
            if hasattr(g,'_Dynamic_last'):
                g._Dynamic_last = g._saved_Dynamic_last.pop()
                g._Dynamic_time = g._saved_Dynamic_time.pop()
            elif hasattr(g,'state_pop') and isinstance(g,Parameterized):
                g.state_pop()



def print_all_param_defaults():
    """Print the default values for all imported Parameters."""
    print("_______________________________________________________________________________")
    print("")
    print("                           Parameter Default Values")
    print("")
    classes = descendents(Parameterized)
    classes.sort(key=lambda x:x.__name__)
    for c in classes:
        c.print_param_defaults()
    print("_______________________________________________________________________________")



# As of Python 2.6+, a fn's **args no longer has to be a
# dictionary. This might allow us to use a decorator to simplify using
# ParamOverrides (if that does indeed make them simpler to use).
# http://docs.python.org/whatsnew/2.6.html
class ParamOverrides(dict):
    """
    A dictionary that returns the attribute of a specified object if
    that attribute is not present in itself.

    Used to override the parameters of an object.
    """

    # NOTE: Attribute names of this object block parameters of the
    # same name, so all attributes of this object should have names
    # starting with an underscore (_).

    def __init__(self,overridden,dict_,allow_extra_keywords=False):
        """

        If allow_extra_keywords is False, then all keys in the
        supplied dict_ must match parameter names on the overridden
        object (otherwise a warning will be printed).

        If allow_extra_keywords is True, then any items in the
        supplied dict_ that are not also parameters of the overridden
        object will be available via the extra_keywords() method.
        """
        # This method should be fast because it's going to be
        # called a lot. This _might_ be faster (not tested):
        #  def __init__(self,overridden,**kw):
        #      ...
        #      dict.__init__(self,**kw)
        self._overridden = overridden
        dict.__init__(self,dict_)

        if allow_extra_keywords:
            self._extra_keywords=self._extract_extra_keywords(dict_)
        else:
            self._check_params(dict_)

    def extra_keywords(self):
        """
        Return a dictionary containing items from the originally
        supplied `dict_` whose names are not parameters of the
        overridden object.
        """
        return self._extra_keywords

    def param_keywords(self):
        """
        Return a dictionary containing items from the originally
        supplied `dict_` whose names are parameters of the
        overridden object (i.e. not extra keywords/parameters).
        """
        return dict((key, self[key]) for key in self if key not in self.extra_keywords())

    def __missing__(self,name):
        # Return 'name' from the overridden object
        return getattr(self._overridden,name)

    def __repr__(self):
        # As dict.__repr__, but indicate the overridden object
        return dict.__repr__(self)+" overriding params from %s"%repr(self._overridden)

    def __getattr__(self,name):
        # Provide 'dot' access to entries in the dictionary.
        # (This __getattr__ method is called only if 'name' isn't an
        # attribute of self.)
        return self.__getitem__(name)

    def __setattr__(self,name,val):
        # Attributes whose name starts with _ are set on self (as
        # normal), but all other attributes are inserted into the
        # dictionary.
        if not name.startswith('_'):
            self.__setitem__(name,val)
        else:
            dict.__setattr__(self,name,val)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key):
        return key in self.__dict__ or key in self._overridden.param

    def _check_params(self,params):
        """
        Print a warning if params contains something that is not a
        Parameter of the overridden object.
        """
        overridden_object_params = list(self._overridden.param)
        for item in params:
            if item not in overridden_object_params:
                self.param.warning("'%s' will be ignored (not a Parameter).",item)

    def _extract_extra_keywords(self,params):
        """
        Return any items in params that are not also
        parameters of the overridden object.
        """
        extra_keywords = {}
        overridden_object_params = list(self._overridden.param)
        for name, val in params.items():
            if name not in overridden_object_params:
                extra_keywords[name]=val
                # Could remove name from params (i.e. del params[name])
                # so that it's only available via extra_keywords()
        return extra_keywords


# Helper function required by ParameterizedFunction.__reduce__
def _new_parameterized(cls):
    return Parameterized.__new__(cls)


class ParameterizedFunction(Parameterized):
    """
    Acts like a Python function, but with arguments that are Parameters.

    Implemented as a subclass of Parameterized that, when instantiated,
    automatically invokes __call__ and returns the result, instead of
    returning an instance of the class.

    To obtain an instance of this class, call instance().
    """
    __abstract = True

    def __str__(self):
        return self.__class__.__name__+"()"

    @bothmethod
    def instance(self_or_cls,**params):
        """
        Return an instance of this class, copying parameters from any
        existing instance provided.
        """

        if isinstance (self_or_cls,ParameterizedMetaclass):
            cls = self_or_cls
        else:
            p = params
            params = self_or_cls.param.values()
            params.update(p)
            params.pop('name')
            cls = self_or_cls.__class__

        inst=Parameterized.__new__(cls)
        Parameterized.__init__(inst,**params)
        if 'name' in params:  inst.__name__ = params['name']
        else:                 inst.__name__ = self_or_cls.name
        return inst

    def __new__(class_,*args,**params):
        # Create and __call__() an instance of this class.
        inst = class_.instance()
        inst.param._set_name(class_.__name__)
        return inst.__call__(*args,**params)

    def __call__(self,*args,**kw):
        raise NotImplementedError("Subclasses must implement __call__.")

    def __reduce__(self):
        # Control reconstruction (during unpickling and copying):
        # ensure that ParameterizedFunction.__new__ is skipped
        state = ParameterizedFunction.__getstate__(self)
        # Here it's necessary to use a function defined at the
        # module level rather than Parameterized.__new__ directly
        # because otherwise pickle will find .__new__'s module to be
        # __main__. Pretty obscure aspect of pickle.py...
        return (_new_parameterized,(self.__class__,),state)

    # PARAM2_DEPRECATION: Remove this compatibility alias for param 2.0 and later; use self.param.pprint instead
    def script_repr(self,imports=[],prefix="    "):
        """
        Same as Parameterized.script_repr, except that X.classname(Y
        is replaced with X.classname.instance(Y
        """
        return self.pprint(imports,prefix,unknown_value='',qualify=True,
                           separator="\n")


    def _pprint(self, imports=None, prefix="\n    ",unknown_value='<?>',
                qualify=False, separator=""):
        """
        Same as Parameterized._pprint, except that X.classname(Y
        is replaced with X.classname.instance(Y
        """
        r = Parameterized._pprint(self,imports,prefix,
                                  unknown_value=unknown_value,
                                  qualify=qualify,separator=separator)
        classname=self.__class__.__name__
        return r.replace(".%s("%classname,".%s.instance("%classname)



class default_label_formatter(ParameterizedFunction):
    "Default formatter to turn parameter names into appropriate widget labels."

    capitalize = Parameter(default=True, doc="""
        Whether or not the label should be capitalized.""")

    replace_underscores = Parameter(default=True, doc="""
        Whether or not underscores should be replaced with spaces.""")

    overrides = Parameter(default={}, doc="""
        Allows custom labels to be specified for specific parameter
        names using a dictionary where key is the parameter name and the
        value is the desired label.""")

    def __call__(self, pname):
        if pname in self.overrides:
            return self.overrides[pname]
        if self.replace_underscores:
            pname = pname.replace('_',' ')
        if self.capitalize:
            pname = pname[:1].upper() + pname[1:]
        return pname


label_formatter = default_label_formatter


