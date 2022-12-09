from typing import Callable, Any, Union
from types import FunctionType
import inspect


class attribute(property):

    def __init__(self, 
                fget: Union[Callable[[Any], Any], None] = None, 
                fset: Union[Callable[[Any, Any], None], None] = None, 
                fdel: Union[Callable[[Any], None], None] = None, 
                fval: Union[Callable, None] = None,
                doc: Union[str, None] = None) -> None:
        super().__init__(fget, fset, fdel, doc)
        self.fval = fval 
        
    def validator(self, fval : Union[Callable, None] = None) -> Union[Callable, None]:
        if fval is None:
            if self.fval is not None:
                return self.fval
            else:
                raise NotImplementedError("attempt to access a NoneType validator.")
        if not isinstance(fval, FunctionType):
            raise TypeError("Validator of attribute is not a method.")
        len_signature = len(inspect.signature(fval).parameters)
        assert 0 < len_signature <= 2, "attribute validator must take 1 or 2 arguments, not {}.".format(len_signature)
        if hasattr(fval, '__len_signature__'):
            raise AttributeError("if using attribute descriptor, member name __len_signature__ is reserved.")
        fval.__len_signature__ = len_signature
        return type(self)(self.fget, self.fset, self.fdel, fval, self.__doc__)


    def __set__(self, __obj: Any, __value: Any) -> None:
        if self.fval is not None:
            if self.fval.__len_signature__ == 1:
                self.fval(__value)
            else:
                self.fval(__obj, __value)   
        return super().__set__(__obj, __value)


__all__ = ['attribute']