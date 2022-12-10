from operator import attrgetter
from typing import Tuple, Dict, Union, Callable, Any
from functools import partial
from typing import List 

from .serializer import JSONSerialization
from .utils import *

def instance_descriptor(set : Callable[[Any, Any, Any],None]) -> Callable[[Any, Any, Any],None]:
    # If parameter has an instance Parameter, delegate setting
    def _set(self, obj, val):
        instance_param = getattr(obj, '_instance__params', {}).get(self.name)
        if instance_param is not None and self is not instance_param:
            instance_param.__set__(obj, val)
            return
        set(self, obj, val)
    return _set


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

    _serializers = {'json': JSONSerialization}

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
        setattr(self, 'owner', owner)


    def __validate_slots(self) -> None:
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

        # event = Event(what=attribute, name=self.name, obj=None, cls=self.owner,
        #               old=old, new=value, type=None)
        # for watcher in self.watchers[attribute]:
        #     self.owner.param._call_watcher(watcher, event)
        # if not self.owner.param._BATCH_WATCH:
        #     self.owner.param._batch_call_watchers()

    def _on_slot_set(self, slot : str, old : Any, value : Any) -> None:
        """
        Can be overridden on subclasses to handle changes when parameter
        attribute is set.
        """
        if slot == 'name':
            if not optimize:
                self.__validate_slots()
            self.validate(self.default)
      

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
    def validator(self) -> Callable:
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


def get_occupied_slots(instance : 'Parameter') -> List[Any]:
    """
    Return a list of slots for which values have been set.

    (While a slot might be defined, if a value for that slot hasn't
    been set, then it's an AttributeError to request the slot's
    value.)
    """
    return [slot for slot in get_all_slots(type(instance))
            if hasattr(instance,slot)]


def get_all_slots(class_ : 'Parameter') -> List[Any]:
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


class RemoteParameter(Parameter):


    def __init__(self, default: Any = None, doc: str = None, constant: bool = False, 
                    readonly: bool = False, allow_None: bool = False, label: str = None, per_instance: bool = True, 
                    deep_copy: bool = False, class_member: bool = False, 
                    pickle_default_value: bool = True, precedence: float = None):
        super().__init__(default, doc, constant, readonly, allow_None, label, per_instance, deep_copy, class_member, pickle_default_value, precedence)



from .parameterized import Parameterized, ParameterizedMetaclass

__all__ = ['Parameter', 'RemoteParameter', 'get_all_slots', 'get_occupied_slots', ]