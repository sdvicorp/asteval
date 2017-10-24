import uuid


class Symbol:
    def __init__(self, name, value, secret=False):
        self.name = name
        self.value = value
        self.modified = True
        self.secret = secret

    def set(self, value, secret=False):
        self.modified = value != self.value
        self.value = value
        self.secret = secret
        return self.modified

    def __repr__(self):
        return "<Symbol {}: val={}, mod?={}>".format(self.name, '******' if self.secret else self.value, self.modified)

    def __str__(self):
        return repr(self)


class SecretValue:
    """
    Used for internal passing of secrets - it wraps regular values to signal secretness
    """
    def __init__(self, value):
        self.value = value

    def set(self, value):
        self.value = value
        return True

    def get(self):
        return self.value

    def __repr__(self):
        return "<SecretValue>"

    def __str__(self):
        return repr(self)


class Frame:
    def __init__(self, name, initial_symbols=None, filename=''):
        if initial_symbols is None:
            initial_symbols = {}
        self.__name = name
        self.__symbols = {}
        for name, value in initial_symbols.items():
            self.__symbols[name] = Symbol(name, value)

        self.__retval = None
        self.__id = uuid.uuid1().hex[:8]
        self.__lineno = 1
        self.__filename = filename

    def set_symbol(self, name, val, secret=False):
        if name in self.__symbols:
            self.__symbols[name].set(val, secret)
        else:
            self.__symbols[name] = Symbol(name, val, secret)

        return self.__symbols[name].modified

    def is_modified(self, name):
        try:
            return self.__symbols[name].modified
        except KeyError:
            return True

    def is_secret(self, name):
        try:
            return self.__symbols[name].secret
        except KeyError:
            return False

    def set_modified(self, name):
        if name in self.__symbols:
            self.__symbols[name].modified = True
        else:
            self.__symbols[name] = Symbol(name, None)

    def reset_modified(self, name):
        if name in self.__symbols:
            self.__symbols[name].modified = False
        else:
            self.__symbols[name] = Symbol(name, None)

    def remove_symbol(self, name):
        self.__symbols.pop(name)

    def get_symbol_value(self, name):
        return self.__symbols[name].value

    def get_symbol(self, name):
        return self.__symbols[name]

    def get_symbols(self):
        return {name: value.value for name, value in self.__symbols.items()}

    def is_symbol(self, name):
        return name in self.__symbols

    def get_name(self):
        return self.__name

    def set_retval(self, retval):
        self.__retval = retval

    def reset_retval(self):
        self.__retval = None

    def get_retval(self):
        """
        None - not set (end up returning None - but needed to affect control flow)
        ReturnedNone - return None
        other value - return value
        :return:
        """
        return self.__retval

    def get_id(self):
        return self.__id

    def set_lineno(self, lineno):
        self.__lineno = lineno

    def get_lineno(self):
        return self.__lineno

    def set_filename(self, filename):
        self.__filename = filename

    def get_filename(self):
        return self.__filename

    def __repr__(self):
        return "<Frame {}: id={}, fn={}, ({} items)>" \
            .format(self.__name, self.__id, self.__filename, len(self.__symbols))

    def __str__(self):
        return repr(self)
