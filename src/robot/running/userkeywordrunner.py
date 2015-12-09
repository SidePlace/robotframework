#  Copyright 2008-2015 Nokia Solutions and Networks
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

from robot.errors import (ExecutionFailed, ReturnFromKeyword, ExecutionPassed,
                          UserKeywordExecutionFailed, DataError, VariableError,
                          PassExecution)
from robot.result.keyword import Keyword as KeywordResult
from robot.utils import DotDict, prepr, split_tags_from_doc
from robot.variables import is_list_var, VariableAssignment

from .keywordrunner import KeywordRunner
from .timeouts import KeywordTimeout
from .statusreporter import StatusReporter


class UserKeywordRunner(object):

    def __init__(self, handler, name=None):
        self._handler = handler
        self.name = name or handler.name

    @property
    def longname(self):
        libname = self._handler.libname
        return '%s.%s' % (libname, self.name) if libname else self.name

    @property
    def libname(self):
        return self._handler.libname

    @property
    def arguments(self):
        return self._handler.arguments

    def run(self, kw, context):
        assignment = VariableAssignment(kw.assign)
        result = self._get_result(kw, assignment, context.variables)
        with StatusReporter(context, result):
            with assignment.assigner(context) as assigner:
                return_value = self._run(context, kw.args, result)
                assigner.assign(return_value)
                return return_value

    def _get_result(self, kw, assignment, variables):
        handler = self._handler
        doc = variables.replace_string(handler.doc, ignore_errors=True)
        doc, tags = split_tags_from_doc(doc)
        tags = [variables.replace_string(tag, ignore_errors=True)
                for tag in handler.tags] + tags
        return KeywordResult(kwname=self.name or '',
                             libname=handler.libname or '',
                             doc=doc.splitlines()[0] if doc else '',
                             args=kw.args,
                             assign=tuple(assignment),
                             tags=tags,
                             type=kw.type)

    def _run(self, context, args, result):
        variables = context.variables
        timeout = self._get_timeout(self._handler.timeout, result, variables)
        args = self._resolve_arguments(args, variables)
        with context.user_keyword(timeout):
            self._set_arguments(args, context)
            error, return_ = self._execute(context)
            if error and not error.can_continue(context.in_teardown):
                raise error
            return_value = self._get_return_value(variables, return_)
            if error:
                error.return_value = return_value
                raise error
            return return_value

    def _get_timeout(self, timeout, result, variables=None):
        if not timeout:
            return None
        timeout = KeywordTimeout(timeout.value, timeout.message, variables)
        result.timeout = str(timeout)
        return timeout

    def _resolve_arguments(self, arguments, variables=None):
        return self.arguments.resolve(arguments, variables)

    def _set_arguments(self, arguments, context):
        positional, named = arguments
        variables = context.variables
        args, kwargs = self.arguments.map(positional, named, variables)
        self._set_variables(args, kwargs, variables)
        context.output.trace(lambda: self._log_args(variables))

    def _set_variables(self, positional, kwargs, variables):
        before_varargs, varargs = self._split_args_and_varargs(positional)
        for name, value in zip(self.arguments.positional, before_varargs):
            variables['${%s}' % name] = value
        if self.arguments.varargs:
            variables['@{%s}' % self.arguments.varargs] = varargs
        if self.arguments.kwargs:
            variables['&{%s}' % self.arguments.kwargs] = DotDict(kwargs)

    def _split_args_and_varargs(self, args):
        if not self.arguments.varargs:
            return args, []
        positional = len(self.arguments.positional)
        return args[:positional], args[positional:]

    def _execute(self, context):
        if not (self._handler.keywords or self._handler.return_value):
            raise DataError("User keyword '%s' contains no keywords." % self.name)
        error = return_ = pass_ = None
        runner = KeywordRunner(context)
        try:
            runner.run_keywords(self._handler.keywords)
        except ReturnFromKeyword as exception:
            return_ = exception
            error = exception.earlier_failures
        except ExecutionPassed as exception:
            pass_ = exception
            error = exception.earlier_failures
        except ExecutionFailed as exception:
            error = exception
        with context.keyword_teardown(error):
            td_error = self._run_teardown(context)
        if error or td_error:
            error = UserKeywordExecutionFailed(error, td_error)
        return error or pass_, return_

    def _log_args(self, variables):
        args = ['${%s}' % arg for arg in self.arguments.positional]
        if self.arguments.varargs:
            args.append('@{%s}' % self.arguments.varargs)
        if self.arguments.kwargs:
            args.append('&{%s}' % self.arguments.kwargs)
        args = ['%s=%s' % (name, prepr(variables[name])) for name in args]
        return 'Arguments: [ %s ]' % ' | '.join(args)

    def _get_return_value(self, variables, return_):
        ret = self._handler.return_value if not return_ else return_.return_value
        if not ret:
            return None
        contains_list_var = any(is_list_var(item) for item in ret)
        try:
            ret = variables.replace_list(ret)
        except DataError as err:
            raise VariableError('Replacing variables from keyword return value '
                                'failed: %s' % err.message)
        if len(ret) != 1 or contains_list_var:
            return ret
        return ret[0]

    def _run_teardown(self, context):
        if not self._handler.teardown:
            return None
        try:
            name = context.variables.replace_string(self._handler.teardown.name)
        except DataError as err:
            return ExecutionFailed(err.message, syntax=True)
        if name.upper() in ('', 'NONE'):
            return None
        runner = KeywordRunner(context)
        try:
            runner.run_keyword(self._handler.teardown, name)
        except PassExecution:
            return None
        except ExecutionFailed as err:
            return err
        return None

    def dry_run(self, kw, context):
        assignment = VariableAssignment(kw.assign)
        result = self._get_result(kw, assignment, context.variables)
        with StatusReporter(context, result):
            assignment.validate_assignment()
            self._dry_run(context, kw.args, result)

    def _dry_run(self, context, args, result):
        timeout = self._get_timeout(self._handler.timeout, result)
        self._resolve_arguments(args)
        with context.user_keyword(timeout):
            error, _ = self._execute(context)
            if error:
                raise error


class EmbeddedArgsUserKeywordRunner(UserKeywordRunner):

    def __init__(self, handler, name):
        UserKeywordRunner.__init__(self, handler, name)
        match = handler.embedded_name.match(name)
        if not match:
            raise ValueError('Does not match given name')
        self.embedded_args = list(zip(handler.embedded_args, match.groups()))

    def _resolve_arguments(self, args, variables=None):
        # Validates that no arguments given.
        self.arguments.resolve(args, variables)
        if not variables:
            return []
        return [(n, variables.replace_scalar(v)) for n, v in self.embedded_args]

    def _set_arguments(self, embedded_args, context):
        for name, value in embedded_args:
            context.variables['${%s}' % name] = value
        # TODO: Trace log embedded args
