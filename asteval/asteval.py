"""
Safe(ish) evaluator of python expressions, using ast module.
The emphasis here is on mathematical expressions, and so
numpy functions are imported if available and used.

Symbols are held in the Interpreter symtable -- a simple
dictionary supporting a simple, flat namespace.

Expressions can be compiled into ast node and then evaluated
later, using the current values in the
"""

from __future__ import division, print_function
from sys import exc_info, stdout, stderr, version_info
import ast
import math
from time import time

import sys

from .astutils import (FROM_PY, FROM_MATH, FROM_NUMPY, UNSAFE_ATTRS,
                       LOCALFUNCS, NUMPY_RENAMES, op2func, RECURSION_LIMIT,
                       ExceptionHolder, ReturnedNone, valid_symbol_name, quote, code_wrap, MAX_CYCLES)

HAS_NUMPY = False
try:
    # noinspection PyUnresolvedReferences
    import numpy

    HAS_NUMPY = True
except ImportError:
    # print("Warning: numpy not available... functionality will be limited.")
    pass

builtins = __builtins__
if not isinstance(builtins, dict):
    builtins = builtins.__dict__

MAX_EXEC_TIME = 2  # sec


# noinspection PyIncorrectDocstring
class Interpreter:
    """mathematical expression compiler and interpreter.

  This module compiles expressions and statements to AST representation,
  using python's ast module, and then executes the AST representation
  using a dictionary of named object (variable, functions).

  The result is a restricted, simplified version of Python meant for
  numerical caclulations that is somewhat safer than 'eval' because some
  operations (such as 'import' and 'eval') are simply not allowed.  The
  resulting language uses a flat namespace that works on Python objects,
  but does not allow new classes to be defined.

  Many parts of Python syntax are supported, including:
     for loops, while loops, if-then-elif-else conditionals
     try-except (including 'finally')
     function definitions with def
     advanced slicing:    a[::-1], array[-3:, :, ::2]
     if-expressions:      out = one_thing if TEST else other
     list comprehension   out = [sqrt(i) for i in values]

  The following Python syntax elements are not supported:
      Import, Exec, Lambda, Class, Global, Generators,
      Yield, Decorators

  In addition, while many builtin functions are supported, several
  builtin functions are missing ('eval', 'exec', and 'getattr' for
  example) that can be considered unsafe.

  If numpy is installed, many numpy functions are also imported.

  """

    supported_nodes = ('arg', 'assert', 'assign', 'attribute', 'augassign',
                       'binop', 'boolop', 'break', 'call', 'compare',
                       'continue', 'delete', 'dict', 'ellipsis',
                       'excepthandler', 'expr', 'extslice', 'for',
                       'functiondef', 'if', 'ifexp', 'index', 'interrupt',
                       'list', 'listcomp', 'module', 'name', 'nameconstant',
                       'num', 'pass', 'print', 'raise', 'repr', 'return',
                       'slice', 'str', 'subscript', 'try', 'tuple', 'unaryop',
                       'while')

    def __init__(self, symtable=None, writer=None, use_numpy=False, err_writer=None,
                 max_time=MAX_EXEC_TIME):
        self.debugging = False  # Set to True to disable the runtime limiter
        self.writer = writer or stdout
        self.err_writer = err_writer or stderr
        self.start = 0
        self.cycles = 0
        self.max_time = max_time
        self.old_recursion_limit = sys.getrecursionlimit()
        if symtable is None:
            symtable = {}
        self.trace_enabled = True
        self.trace = []
        self.symtable = symtable
        self._interrupt = None
        self.error = []
        self.error_msg = None
        self.expr = None
        self.retval = None
        self.last_error = None
        self.use_numpy = HAS_NUMPY and use_numpy

        symtable['print'] = self.print_

        for sym in FROM_PY:
            if sym in builtins:
                symtable[sym] = builtins[sym]

        for symname, obj in LOCALFUNCS.items():
            symtable[symname] = obj

        for sym in FROM_MATH:
            if hasattr(math, sym):
                symtable[sym] = getattr(math, sym)

        if self.use_numpy:
            for sym in FROM_NUMPY:
                if hasattr(numpy, sym):
                    symtable[sym] = getattr(numpy, sym)
            for name, sym in NUMPY_RENAMES.items():
                if hasattr(numpy, sym):
                    symtable[name] = getattr(numpy, sym)

        self.node_handlers = dict(((node, getattr(self, "on_%s" % node))
                                   for node in self.supported_nodes))

        # to rationalize try/except try/finally for Python2.6 through Python3.3
        self.node_handlers['tryexcept'] = self.node_handlers['try']
        self.node_handlers['tryfinally'] = self.node_handlers['try']

        self.no_deepcopy = []
        for key, val in symtable.items():
            if callable(val) or 'numpy.lib.index_tricks' in repr(val):
                self.no_deepcopy.append(key)

    def set_trace(self, on_off):
        self.trace_enabled = on_off

    def tracer(self, s):
        if self.trace_enabled:
            self.trace.append(s)

    def get_trace(self):
        return self.trace

    def get_errors(self):
        return self.error

    def add_symbol(self, name, value):
        self.symtable[name] = value

    def add_function(self, func):
        if callable(func) and hasattr(func, '__name__'):
            self.symtable[func.__name__] = func

    @staticmethod
    def set_recursion_limit():
        sys.setrecursionlimit(RECURSION_LIMIT)

    def reset_recursion_limit(self):
        sys.setrecursionlimit(self.old_recursion_limit)

    def unimplemented(self, node):
        """unimplemented nodes"""
        self.raise_exception(node, exc=NotImplementedError,
                             msg="`%s` not supported" % node.__class__.__name__)

    @staticmethod
    def getLinenoLabel(node):
        if hasattr(node, 'lineno'):
            return "Line {}: ".format(node.lineno)
        return ''

    @staticmethod
    def getLineno(node):
        if hasattr(node, 'lineno'):
            return node.lineno

    def raise_exception(self, node, exc=None, msg='', expr=None, lineno=None):
        """add an exception"""
        if self.error is None:
            self.error = []
        if expr is None:
            expr = self.expr
        if self.error and not isinstance(node, ast.Module):
            msg = str(msg)
        if lineno is None:
            lineno = self.getLineno(node)
        err = ExceptionHolder(node, exc=exc, msg=msg, expr=expr, lineno=lineno)
        self._interrupt = ast.Break()
        self.error.append(err)
        if self.error_msg is None:
            self.error_msg = "%s in `%s`" % (msg, self.expr)
        elif msg:
            self.error_msg = "%s\n %s" % (self.error_msg, msg)
        if exc is None:
            # noinspection PyBroadException
            try:
                exc = self.error[0].exc
            except:
                exc = RuntimeError
        raise exc(self.error_msg)

    def __call__(self, expr, **kw):
        return self.eval(expr, **kw)

    def eval(self, expr, show_errors=True, limit_recursion=False):
        """evaluates a single or block of statements or whole file"""
        # self.lineno = lineno
        self.error = []
        self.trace = []
        self.start = time()
        self.cycles = 0
        if limit_recursion:
            self.set_recursion_limit()
        try:
            # noinspection PyBroadException
            try:
                node = self.parse(expr, limit_recursion=limit_recursion)
            except:
                errmsg = exc_info()[1]
                if self.error:
                    errmsg = "\n".join(self.error[0].get_error())
                if not show_errors:
                    # noinspection PyBroadException
                    try:
                        exc = self.error[0].exc
                    except:
                        exc = RuntimeError
                    raise exc(errmsg)
                print(errmsg, file=self.err_writer)
                return
            # noinspection PyBroadException
            try:
                return self.run(node, expr=expr)
            except:
                errmsg = exc_info()[1]
                if self.error:
                    errmsg = "\n".join(self.error[0].get_error())
                if not show_errors:
                    # noinspection PyBroadException
                    try:
                        exc = self.error[0].exc
                    except:
                        exc = RuntimeError
                    raise exc(errmsg)
                print(errmsg, file=self.err_writer)
                return
        finally:
            if limit_recursion:
                self.reset_recursion_limit()

    # main entry point for Ast node evaluation
    #  parse:  text of statements -> ast
    #  run:    ast -> result
    #  eval:   string statement -> result = run(parse(statement))
    def parse(self, text, limit_recursion=False):
        """parse statement/expression to Ast representation"""
        self.expr = text

        # noinspection PyBroadException
        if limit_recursion:
            self.set_recursion_limit()
        try:
            return ast.parse(text)
        except SyntaxError as e:
            # self.tracer(str(e))
            self.raise_exception(None, msg='Syntax Error: {}'.format(e), expr=text, lineno=e.lineno)
        except Exception as e:
            # self.tracer(str(e))
            self.raise_exception(None, msg='Runtime Error: {}'.format(e), expr=text)
        finally:
            if limit_recursion:
                self.reset_recursion_limit()

    def run(self, node, expr=None, with_raise=True):
        """executes parsed Ast representation for an expression"""
        # Note: keep the 'node is None' test: internal code here may run
        #    run(None) and expect a None in return.
        if not self.debugging and time() - self.start > self.max_time:
            raise RuntimeError("Execution exceeded time limit, max runtime is {}s".format(MAX_EXEC_TIME))
        self.cycles += 1
        if self.cycles > MAX_CYCLES:
            raise RuntimeError("Max cycles exceeded, max is {}.".format(MAX_CYCLES))
        if self.error:
            return
        if node is None:
            return
        if isinstance(node, str):
            node = self.parse(node)
        # if lineno is not None:
        #     self.lineno = lineno
        if expr is not None:
            self.expr = expr

        # get handler for this node:
        #   on_xxx with handle nodes of type 'xxx', etc
        try:
            handler = self.node_handlers[node.__class__.__name__.lower()]
        except KeyError:
            return self.unimplemented(node)

        # run the handler:  this will likely generate
        # recursive calls into this run method.
        # noinspection PyBroadException
        try:
            #self.tracer("Calling `{}({})`...".format(handler.__name__, node))
            ret = handler(node)
            if isinstance(ret, enumerate):
                ret = list(ret)
            return ret
        except:
            if with_raise:
                self.raise_exception(node, expr=expr)

    @staticmethod
    def dump(node, **kw):
        """simple ast dumper"""
        return ast.dump(node, **kw)

    # handlers for ast components
    def on_expr(self, node):
        """expression"""
        val = self.run(node.value)
        self.tracer("{}Expression returned `{}`.".format(self.getLinenoLabel(node), quote(val)))
        return val  # ('value',)

    def on_index(self, node):
        """index"""
        return self.run(node.value)  # ('value',)

    def on_return(self, node):  # ('value',)
        """return statement: look for None, return special sentinal"""
        self.retval = self.run(node.value)
        if self.retval is None:
            self.retval = ReturnedNone

    def on_repr(self, node):
        """repr """
        return repr(self.run(node.value))  # ('value',)

    def on_module(self, node):  # ():('body',)
        """module def"""
        out = None
        for tnode in node.body:
            out = self.run(tnode)
        return out

    # noinspection PyMethodMayBeStatic,PyUnusedLocal
    def on_pass(self, node):
        """pass statement"""
        pass  # ()

    # noinspection PyMethodMayBeStatic,PyUnusedLocal
    def on_ellipsis(self, node):
        """ellipses"""
        return Ellipsis

    # for break and continue: set the instance variable _interrupt
    def on_interrupt(self, node):  # ()
        """interrupt handler"""
        self._interrupt = node
        return node

    def on_break(self, node):
        """break"""
        return self.on_interrupt(node)

    def on_continue(self, node):
        """continue"""
        return self.on_interrupt(node)

    def on_assert(self, node):  # ('test', 'msg')
        """assert statement"""
        if not self.run(node.test):
            self.raise_exception(node, exc=AssertionError, msg=node.msg)
        return True

    def on_list(self, node):  # ('elt', 'ctx')
        """list"""
        return [self.run(e) for e in node.elts]

    def on_tuple(self, node):  # ('elts', 'ctx')
        """tuple"""
        return tuple(self.on_list(node))

    def on_dict(self, node):  # ('keys', 'values')
        """dictionary"""
        return dict([(self.run(k), self.run(v)) for k, v in
                     zip(node.keys, node.values)])

    # noinspection PyMethodMayBeStatic
    def on_num(self, node):  # ('n',)
        """return number"""
        return node.n

    # noinspection PyMethodMayBeStatic
    def on_str(self, node):  # ('s',)
        """return string"""
        return node.s

    def on_name(self, node):  # ('id', 'ctx')
        """ Name node """
        ctx = node.ctx.__class__
        if ctx in (ast.Param, ast.Del):
            return str(node.id)
        else:
            if node.id in self.symtable:
                val = self.symtable[node.id]
                val_str = repr(val)
                if not val_str.startswith('<'):
                    self.tracer("{}Value of `{}` is `{}`.".format(self.getLinenoLabel(node), node.id, code_wrap(val)))
                return val
            else:
                msg = "name `%s` is not defined" % node.id
                self.raise_exception(node, exc=NameError, msg=msg)

    # noinspection PyMethodMayBeStatic
    def on_nameconstant(self, node):
        """ True, False, None in python >= 3.4 """
        return node.value

    def node_assign(self, node, val):
        """here we assign a value (not the node.value object) to a node
        this is used by on_assign, but also by for, list comprehension, etc.
        """
        if node.__class__ == ast.Name:
            if not valid_symbol_name(node.id):
                errmsg = "invalid symbol name (reserved word?) `%s`" % node.id
                self.raise_exception(node, exc=NameError, msg=errmsg)
            self.symtable[node.id] = val
            self.tracer("{}Assigned value of {} to `{}`.".format(self.getLinenoLabel(node), code_wrap(val), node.id))
            if node.id in self.no_deepcopy:
                self.no_deepcopy.remove(node.id)

        elif node.__class__ == ast.Attribute:
            if node.ctx.__class__ == ast.Load:
                msg = "cannot assign to attribute `%s`" % node.attr
                self.raise_exception(node, exc=AttributeError, msg=msg)

            setattr(self.run(node.value), node.attr, val)

        elif node.__class__ == ast.Subscript:
            sym = self.run(node.value)
            xslice = self.run(node.slice)
            if isinstance(node.slice, ast.Index):
                sym[xslice] = val
            elif isinstance(node.slice, ast.Slice):
                # noinspection PyTypeChecker
                sym[slice(xslice.start, xslice.stop)] = val
            elif isinstance(node.slice, ast.ExtSlice):
                sym[xslice] = val
        elif node.__class__ in (ast.Tuple, ast.List):
            if len(val) == len(node.elts):
                for telem, tval in zip(node.elts, val):
                    self.node_assign(telem, tval)
            else:
                raise ValueError('too many values to unpack')

        elif isinstance(node, str):
            self.symtable[node] = val

        else:
            raise ValueError("Invalid node assignment type!")

    def on_attribute(self, node):  # ('value', 'attr', 'ctx')
        """extract attribute"""
        ctx = node.ctx.__class__
        if ctx == ast.Store:
            msg = "attribute for storage: shouldn't be here!"
            self.raise_exception(node, exc=RuntimeError, msg=msg)

        sym = self.run(node.value)
        if ctx == ast.Del:
            return delattr(sym, node.attr)

        # ctx is ast.Load
        fmt = "cannnot access attribute `%s` for `%s`"
        if node.attr not in UNSAFE_ATTRS:
            fmt = "no attribute `%s` for `%s`"
            try:
                return getattr(sym, node.attr)
            except AttributeError:
                pass

        # AttributeError or accessed unsafe attribute
        obj = self.run(node.value)
        msg = fmt % (node.attr, obj)
        self.raise_exception(node, exc=AttributeError, msg=msg)

    def on_assign(self, node):  # ('targets', 'value')
        """simple assignment"""
        val = self.run(node.value)
        for tnode in node.targets:
            self.node_assign(tnode, val)
        return

    def on_augassign(self, node):  # ('target', 'op', 'value')
        """augmented assign"""
        return self.on_assign(ast.Assign(targets=[node.target],
                                         value=ast.BinOp(left=node.target,
                                                         op=node.op,
                                                         right=node.value)))

    def on_slice(self, node):  # ():('lower', 'upper', 'step')
        """simple slice"""
        return slice(self.run(node.lower),
                     self.run(node.upper),
                     self.run(node.step))

    def on_extslice(self, node):  # ():('dims',)
        """extended slice"""
        return tuple([self.run(tnode) for tnode in node.dims])

    def on_subscript(self, node):  # ('value', 'slice', 'ctx')
        """subscript handling -- one of the tricky parts"""
        val = self.run(node.value)
        nslice = self.run(node.slice)
        ctx = node.ctx.__class__
        if ctx in (ast.Load, ast.Store):
            if isinstance(node.slice, (ast.Index, ast.Slice, ast.Ellipsis)):
                return val.__getitem__(nslice)
            elif isinstance(node.slice, ast.ExtSlice):
                return val[nslice]
        else:
            msg = "subscript with unknown context"
            self.raise_exception(node, msg=msg)

    def on_delete(self, node):  # ('targets',)
        """delete statement"""
        for tnode in node.targets:
            if tnode.ctx.__class__ != ast.Del:
                break
            children = []
            while tnode.__class__ == ast.Attribute:
                children.append(tnode.attr)
                tnode = tnode.value

            if tnode.__class__ == ast.Name:
                children.append(tnode.id)
                children.reverse()
                self.symtable.pop('.'.join(children))
            else:
                msg = "could not delete symbol"
                self.raise_exception(node, msg=msg)

    def on_unaryop(self, node):  # ('op', 'operand')
        """unary operator"""
        func, name = op2func(node.op)
        val = self.run(node.operand)
        ret = func(val)
        return ret

    def on_binop(self, node):  # ('left', 'op', 'right')
        """binary operator"""
        func, name = op2func(node.op)
        left, right = self.run(node.left), self.run(node.right)
        ret = func(left, right)
        self.tracer("{}Operation `{} {} {}` returned `{}`."
                    .format(self.getLinenoLabel(node), quote(left), name, quote(right), quote(ret)))
        return ret

    def on_boolop(self, node):  # ('op', 'values')
        """boolean operator"""
        val = self.run(node.values[0])
        is_and = ast.And == node.op.__class__
        if (is_and and val) or (not is_and and not val):
            for n in node.values[1:]:
                func, _ = op2func(node.op)
                val = func(val, self.run(n))
                if (is_and and not val) or (not is_and and val):
                    break
        self.tracer("{}Boolean expression returned `{}`.".format(self.getLinenoLabel(node), val))
        return val

    def on_compare(self, node):  # ('left', 'ops', 'comparators')
        """comparison operators"""
        lval = self.run(node.left)
        out = True
        for op, rnode in zip(node.ops, node.comparators):
            rval = self.run(rnode)
            func, name = op2func(op)
            out = func(lval, rval)
            self.tracer("{}Comparison `{} {} {}` returned `{}`."
                        .format(self.getLinenoLabel(node), quote(lval), name, quote(rval), quote(out)))
            lval = rval
            if self.use_numpy and isinstance(out, numpy.ndarray) and out.any():
                break
            elif not out:
                break
        return out

    def on_print(self, node):  # ('dest', 'values', 'nl')
        """ note: implements Python2 style print statement, not
        print() function.  May need improvement...."""
        dest = self.run(node.dest) or self.writer
        end = ''
        if node.nl:
            end = '\n'
        out = [self.run(tnode) for tnode in node.values]
        if out:  # and not self.error:
            self.print_(*out, file=dest, end=end)

    def print_(self, *out, **kws):
        """generic print function"""
        flush = kws.pop('flush', True)
        fileh = kws.pop('file', self.writer)
        sep = kws.pop('sep', ' ')
        end = kws.pop('sep', '\n')

        print(*out, file=fileh, sep=sep, end=end)
        if flush:
            fileh.flush()

    print_.__name__ = 'print'

    def on_if(self, node):  # ('test', 'body', 'orelse')
        """regular if-then-else statement"""
        block = node.body
        if not self.run(node.test):
            block = node.orelse
        for tnode in block:
            self.run(tnode)

    def on_ifexp(self, node):  # ('test', 'body', 'orelse')
        """if expressions"""
        expr = node.orelse
        if self.run(node.test):
            expr = node.body
        return self.run(expr)

    def on_while(self, node):  # ('test', 'body', 'orelse')
        """while blocks"""
        while self.run(node.test):
            self._interrupt = None
            for tnode in node.body:
                self.run(tnode)
                if self._interrupt is not None:
                    break
            if isinstance(self._interrupt, ast.Break):
                break
        else:
            for tnode in node.orelse:
                self.run(tnode)
        self._interrupt = None

    def on_for(self, node):  # ('target', 'iter', 'body', 'orelse')
        """for blocks"""
        for val in self.run(node.iter):
            self.node_assign(node.target, val)
            self._interrupt = None
            for tnode in node.body:
                self.run(tnode)
                if self._interrupt is not None:
                    break
            if isinstance(self._interrupt, ast.Break):
                break
        else:
            for tnode in node.orelse:
                self.run(tnode)
        self._interrupt = None

    def on_listcomp(self, node):  # ('elt', 'generators')
        """list comprehension"""
        out = []
        for tnode in node.generators:
            if tnode.__class__ == ast.comprehension:
                for val in self.run(tnode.iter):
                    self.node_assign(tnode.target, val)
                    add = True
                    for cond in tnode.ifs:
                        add = add and self.run(cond)
                    if add:
                        out.append(self.run(node.elt))
        return out

    def on_excepthandler(self, node):  # ('type', 'name', 'body')
        """exception handler"""
        if node.name is not None and self.last_error:  # set the `as` variable if specified
            self.node_assign(node.name, self.last_error)
        for ebody in node.body:  # run the statements in the handler body
            self.run(ebody, with_raise=False)

    def on_try(self, node):  # ('body', 'handlers', 'orelse', 'finalbody')
        """try/except/else/finally blocks"""
        no_errors = True
        for tnode in node.body:
            self.run(tnode, with_raise=False)
            no_errors = no_errors and not self.error
            if self.error:
                self.last_error = self.error[-1].exc_info[1]
                self.error = []
                for hnd in node.handlers:
                    self.run(hnd)
                self.last_error = None
                break

        if no_errors and hasattr(node, 'orelse'):
            self.tracer('{}Executing `else` block.'.format(self.getLinenoLabel(node.orelse)))
            for tnode in node.orelse:
                self.run(tnode)

        if hasattr(node, 'finalbody'):
            self.tracer('{}Executing `finally` block.'.format(self.getLinenoLabel(node.finalbody)))
            for tnode in node.finalbody:
                self.run(tnode)

    def on_raise(self, node):  # ('type', 'inst', 'tback')
        """raise statement: note difference for python 2 and 3"""
        if version_info[0] == 3:
            excnode = node.exc
            msgnode = node.cause
        else:
            excnode = node.type
            msgnode = node.inst
        out = self.run(excnode)
        msg = ' '.join(out.args)
        msg2 = self.run(msgnode)
        if msg2 not in (None, 'None'):
            msg = "`%s: %s`" % (msg, msg2)
        self.raise_exception(None, exc=out.__class__, msg=msg, expr='')

    def on_call(self, node):
        """function execution"""
        #  ('func', 'args', 'keywords', and 'starargs', 'kwargs' in py < 3.5)
        func = self.run(node.func)

        if not hasattr(func, '__call__') and not isinstance(func, type):
            msg = "`%s` is not callable!!" % func
            self.raise_exception(node, exc=TypeError, msg=msg)

        args = [self.run(targ) for targ in node.args]
        starargs = getattr(node, 'starargs', None)
        if starargs is not None:
            args = args + self.run(starargs)

        keywords = {}
        for key in node.keywords:
            if not isinstance(key, ast.keyword):
                msg = "keyword error in function call `%s`" % func
                self.raise_exception(node, msg=msg)

            keywords[key.arg] = self.run(key.value)

        kwargs = getattr(node, 'kwargs', None)
        if kwargs is not None:
            keywords.update(self.run(kwargs))

        name = ''
        if hasattr(func, '__name__'):
            name = func.__name__
        elif hasattr(func, 'name'):
            name = func.name

        arg_list = []
        if args:
            arg_list.append(', '.join([quote(arg) for arg in args]))
        if keywords:
            arg_list.append(', '.join(['{}={}'.format(k, quote(v)) for (k, v) in keywords.items()]))

        arg_str = ', '.join(arg_list)

        # noinspection PyBroadException
        try:
            ret = func(*args, **keywords)
        except Exception as e:
            self.tracer('{}Function `{}({})` raised on exception `{}`.'
                        .format(self.getLinenoLabel(node), name, arg_str, code_wrap(str(e))))

            self.raise_exception(node, msg="Error calling `%s()`: `%s`" % (name, str(e)))
            return

        self.tracer('{}Function `{}({})` returned `{}`.'
                    .format(self.getLinenoLabel(node), name, arg_str, code_wrap(ret)))
        return ret

    # noinspection PyMethodMayBeStatic
    def on_arg(self, node):  # ('test', 'msg')
        """arg for function definitions"""
        return node.arg

    def on_functiondef(self, node):
        """define procedures"""
        # ('name', 'args', 'body', 'decorator_list')
        if node.decorator_list:
            raise Warning("decorated procedures not supported!")
        kwargs = []

        offset = len(node.args.args) - len(node.args.defaults)
        for idef, defnode in enumerate(node.args.defaults):
            defval = self.run(defnode)
            keyval = self.run(node.args.args[idef + offset])
            kwargs.append((keyval, defval))

        if version_info[0] == 3:
            args = [tnode.arg for tnode in node.args.args[:offset]]
        else:
            args = [tnode.id for tnode in node.args.args[:offset]]

        doc = None
        nb0 = node.body[0]
        if isinstance(nb0, ast.Expr) and isinstance(nb0.value, ast.Str):
            doc = nb0.value.s

        varkws = node.args.kwarg
        vararg = node.args.vararg
        if version_info[0] == 3:
            if isinstance(vararg, ast.arg):
                vararg = vararg.arg
            if isinstance(varkws, ast.arg):
                varkws = varkws.arg

        self.symtable[node.name] = Procedure(node.name, self, doc=doc,
                                             # lineno=self.lineno,
                                             lineno=node.lineno,
                                             body=node.body,
                                             args=args, kwargs=kwargs,
                                             vararg=vararg, varkws=varkws)
        if node.name in self.no_deepcopy:
            self.no_deepcopy.pop(node.name)


class Procedure(object):
    """Procedure: user-defined function for asteval

    This stores the parsed ast nodes as from the
    'functiondef' ast node for later evaluation.
    """

    def __init__(self, name, interp, doc=None, lineno=0,
                 body=None, args=None, kwargs=None,
                 vararg=None, varkws=None):
        self.name = name
        self.__name__ = name
        self.__asteval__ = interp
        self.raise_exc = self.__asteval__.raise_exception
        self.__doc__ = doc
        self.body = body
        self.argnames = args
        self.kwargs = kwargs
        self.vararg = vararg
        self.varkws = varkws
        self.lineno = lineno

    def __repr__(self):
        sig = ""
        if self.argnames:
            sig = "%s%s" % (sig, ', '.join(self.argnames))
        if self.vararg is not None:
            sig = "%s, *%s" % (sig, self.vararg)
        if self.kwargs:
            if sig:
                sig = "%s, " % sig
            _kw = ["%s=%s" % (k, v) for k, v in self.kwargs]
            sig = "%s%s" % (sig, ', '.join(_kw))

        if self.varkws is not None:
            sig = "%s, **%s" % (sig, self.varkws)
        sig = "<Procedure %s(%s)>" % (self.name, sig)
        if self.__doc__ is not None:
            sig = "%s\n  %s" % (sig, self.__doc__)
        return sig

    def __call__(self, *args, **kwargs):
        symlocals = {}
        args = list(args)
        n_args = len(args)
        n_names = len(self.argnames)
        n_kws = len(kwargs)

        # may need to move kwargs to args if names align!
        if (n_args < n_names) and n_kws > 0:
            for name in self.argnames[n_args:]:
                if name in kwargs:
                    args.append(kwargs.pop(name))
            n_args = len(args)
            n_names = len(self.argnames)

        if self.argnames and kwargs is not None:
            msg = "multiple values for keyword argument `%s` in Procedure `%s`"
            for targ in self.argnames:
                if targ in kwargs:
                    self.raise_exc(None, exc=TypeError,
                                   msg=msg % (targ, self.name),
                                   lineno=self.lineno)

        if n_args != n_names:
            if n_args < n_names:
                msg = 'not enough arguments for Procedure `%s()`' % self.name
                msg = '`%s` (expected `%i`, got `%i`)' % (msg, n_names, n_args)
                self.raise_exc(None, exc=TypeError, msg=msg)

        for argname in self.argnames:
            symlocals[argname] = args.pop(0)

        try:
            if self.vararg is not None:
                symlocals[self.vararg] = tuple(args)

            for key, val in self.kwargs:
                if key in kwargs:
                    val = kwargs.pop(key)
                symlocals[key] = val

            if self.varkws is not None:
                symlocals[self.varkws] = kwargs

            elif kwargs:
                msg = 'extra keyword arguments for Procedure `%s` (`%s`)'
                msg = msg % (self.name, ','.join(list(kwargs.keys())))
                self.raise_exc(None, msg=msg, exc=TypeError,
                               lineno=self.lineno)

        except (ValueError, LookupError, TypeError,
                NameError, AttributeError):
            msg = 'incorrect arguments for Procedure `%s`' % self.name
            self.raise_exc(None, msg=msg, lineno=self.lineno)

        save_symtable = self.__asteval__.symtable.copy()
        self.__asteval__.symtable.update(symlocals)
        self.__asteval__.retval = None
        retval = None

        # evaluate script of function
        for node in self.body:
            self.__asteval__.run(node, expr='<>')
            if self.__asteval__.error:
                break
            if self.__asteval__.retval is not None:
                retval = self.__asteval__.retval
                if retval is ReturnedNone:
                    retval = None
                break

        self.__asteval__.symtable = save_symtable
        return retval
