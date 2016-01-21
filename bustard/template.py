# -*- coding: utf-8 -*-
"""
简单的模板引擎

支持

* 直接输出变量：{{ foobar }}
* 注释: {# ... #}
* if 语句：{% if xx %} {% elif yy %} {% else %} {% endif %}
* for 循环：{% for x in lst %} {% endfor %}
* 内置函数: {{ '  foobar  '.strip() }}
* 访问对象的属性和方法: {{ foo.bar }} {{ foo.hello() }}
* 字典或列表索引: {{ foo['bar'] }}
* include: {% include "path/to/b.tpl" %}

"""
import builtins
import os
import re

from .constants import TEMPLATE_BUILTIN_FUNC_WHITELIST
from .utils import to_text


class CodeBuilder(object):
    # 缩进步长
    INDENT_STEP = 4

    def __init__(self, indent=0):
        self.source_code = []
        # 当前缩进
        self.indent_level = indent

    def add_line(self, line):
        """增加一行代码"""
        line = ' ' * self.indent_level + line + '\n'
        self.source_code.extend([line])

    def forward_indent(self):
        """缩进前进一步"""
        self.indent_level += self.INDENT_STEP

    def back_indent(self):
        """缩进后退一步"""
        self.indent_level -= self.INDENT_STEP

    def add_section(self):
        """申请一个基于当前缩进的代码块"""
        section = CodeBuilder(self.indent_level)
        self.source_code.append(section)
        return section

    def _compile(self):
        """编译生成的代码"""
        assert self.indent_level == 0
        self._code = compile(str(self), '<source>', 'exec')
        return self._code

    def get_namespace(self):
        """执行生成的代码"""
        namespace = {}
        exec(self._code, namespace)
        return namespace

    def __str__(self):
        return ''.join(str(s) for s in self.source_code)


class Template(object):
    TOKEN_EXPR_START = '{{'
    TOKEN_EXPR_END = '}}'
    TOKEN_TAG_START = '{%'
    TOKEN_TAG_END = '%}'
    TOKEN_COMMENT_START = '{#'
    TOKEN_COMMENT_END = '#}'
    FUNC_WHITELIST = TEMPLATE_BUILTIN_FUNC_WHITELIST

    def __init__(self, text, context=None,
                 pre_compile=True,
                 indent=0, template_dir='',
                 up_vars=None,
                 func_name='render_function',
                 result_var='result',
                 auto_escape=True
                 ):
        self.tokens_re = re.compile(r'''(?sx)(
        (?:{token_expr_start}.*?{token_expr_end})
        |(?:{token_tag_start}.*?{token_tag_end})
        |(?:{token_comment_start}.*?{token_comment_end})
        )'''.format(token_expr_start=re.escape(self.TOKEN_EXPR_START),
                    token_expr_end=re.escape(self.TOKEN_EXPR_END),
                    token_tag_start=re.escape(self.TOKEN_TAG_START),
                    token_tag_end=re.escape(self.TOKEN_TAG_END),
                    token_comment_start=re.escape(self.TOKEN_COMMENT_START),
                    token_comment_end=re.escape(self.TOKEN_COMMENT_END),
                    )
        )

        self.context = {k: v
                        for k, v in builtins.__dict__.items()
                        if k in self.FUNC_WHITELIST
                        }
        self.context.update({
            'escape': escape,
            'noescape': noescape,
            'to_text': noescape,
        })
        self.base_dir = template_dir
        self.func_name = func_name
        self.result_var = result_var
        self.auto_escape = auto_escape
        # 上一层定义过的变量
        self.up_vars = up_vars or set()
        if context is not None:
            self.context.update(context)

        self.buffered = []
        self.code = code = CodeBuilder(indent=indent)
        code.add_line('def %s(context):' % func_name)
        code.forward_indent()

        # 定义 context 内的变量
        self.section_vars = code.add_section()
        # 将函数内的执行结果保存在 result 中
        code.add_line('%s = []' % result_var)
        # escape, noescape
        code.add_line('escape = context["escape"]')
        code.add_line('noescape = context["noescape"]')
        code.add_line('to_text = context["to_text"]')

        self.tpl_text = text
        # 模板中出现过的全局变量
        self.global_vars = set()
        # 模板中定义的变量
        self.tmp_vars = set()

        # 解析模板
        self.parse_text(text)

        if pre_compile:
            # 编译生成的代码
            self.code._compile()
            namespace = self.code.get_namespace()
            self.render_function = namespace[func_name]

    def parse_text(self, text):
        tokens = self.tokens_re.split(text)
        # express_stack = []

        for token in tokens:
            # 普通字符串
            if not self.tokens_re.match(token):
                self.buffered.append('%s' % repr(token))
            # {# ... #}
            elif token.startswith(self.TOKEN_COMMENT_START):
                continue
            # {{ abc }}
            elif token.startswith(self.TOKEN_EXPR_START):
                global_var = self.strip_token(token, self.TOKEN_EXPR_START,
                                              self.TOKEN_EXPR_END).strip()
                global_var = self.collect_var(global_var)

                if self.auto_escape:
                    self.buffered.append(
                        'escape(%s)' % self.wrap_var(global_var)
                    )
                else:
                    self.buffered.append(
                        'to_text(%s)' % self.wrap_var(global_var)
                    )

            # {% blala %}
            elif token.startswith(self.TOKEN_TAG_START):
                self.flush_buffer()
                express = self.strip_token(token, self.TOKEN_TAG_START,
                                           self.TOKEN_TAG_END)
                words = express.split()
                if words[0] == 'if':   # {% if xx %}
                    global_var = self.collect_var(' '.join(words[1:]))

                    self.code.add_line('if %s:' % self.wrap_var(global_var))
                    self.code.forward_indent()
                elif words[0] == 'elif':  # {% elif xx %}
                    self.code.back_indent()
                    global_var = self.collect_var(' '.join(words[1:]))

                    self.code.add_line('elif %s:' % self.wrap_var(global_var))
                    self.code.forward_indent()
                elif words[0] == 'else':  # {% else %}
                    self.code.back_indent()
                    self.code.add_line('else:')
                    self.code.forward_indent()

                elif words[0] == 'for':  # {% for x in y %}
                    in_index = words.index('in')
                    tmp_var = self.collect_tmp_var(' '.join(words[1:in_index]))
                    global_var = self.collect_var(
                        ' '.join(words[in_index + 1:]),
                    )

                    self.code.add_line('for %s in %s:'
                                       % (self.wrap_var(tmp_var), global_var))
                    self.code.forward_indent()

                elif words[0].startswith('end'):  # {% endif %}, {% endfor %}
                    if words[0] == 'endfor':   # 排除循环过程中产生的临时变量
                        self.global_vars = self.global_vars - self.tmp_vars
                        self.tmp_vars.clear()
                    self.code.back_indent()

                elif words[0] == 'include':
                    # 保存当前 locals
                    path = ''.join(words[1:]).strip().strip('\'"')
                    func_name, _code = self.handle_include(path)
                    self.code.source_code.append(_code)
                    self.code.add_line('%s.append(%s(context))'
                                       % (self.result_var, func_name))

        self.define_global_vars()

        self.flush_buffer()
        self.code.add_line('return "".join(%s)' % self.result_var)
        self.code.back_indent()

    def define_global_vars(self):
        # 定义模板中用到的全局变量
        for name in (self.global_vars - self.tmp_vars):
            if name not in self.up_vars:
                self.section_vars.add_line('%s = context["%s"]' % (name, name))

    def render(self, **context):
        """使用 context 字典渲染模板"""
        _context = {}
        _context.update(self.context)
        if context is not None:
            _context.update(context)

        return self.render_function(_context)

    def handle_include(self, path):
        path = os.path.join(self.base_dir, path)
        up_vars = set()
        up_vars.update(self.up_vars)
        up_vars.update(self.global_vars)
        with open(path, encoding='utf-8') as f:
            _code = Template(
                f.read(), self.context,
                pre_compile=False, indent=self.code.indent_level,
                template_dir=self.base_dir,
                up_vars=up_vars, auto_escape=self.auto_escape
            ).code
            return self.func_name, _code

    def collect_var(self, var):
        """将模板中出现的变量加入到 global_vars 中"""
        var = var.strip()
        self._collect_var(var, self.global_vars)
        return var

    def collect_tmp_var(self, var):
        """收集循环中定义的临时变量"""
        var = var.strip()
        self._collect_var(var, self.tmp_vars)
        return var

    def _collect_var(self, var, collect):
        # 不处理 {{ "abc" }}
        if (not var) or var.startswith('"') or var.startswith('\''):
            return

        _vars = re.split(r'[,\s\(\)\[\]]+', var)
        if len(_vars) > 1:   # {% if len(foobar) %}
            for _var in _vars:
                _var = _var.strip()
                # {{ foobar(abc=1) }}, a[2]
                if (re.match(r'^\w+\s*=[\'"\d]', _var) or
                        re.match(r'^\d', _var)):
                    continue
                # {{ foobar(abc=efg) }}
                elif re.match(r'^\w+\s*=', _var):
                    _var = _var.split('=')[1]
                self._collect_var(_var, collect)
        elif re.match(r'^[a-zA-Z_](\w+)?', _vars[0]):
            _var = _vars[0].split('.')[0]
            collect.add(_var)

    def wrap_var(self, var):
        """处理变量, 将临时变量的名称增加 _ 前缀"""
        var = var.strip()
        self.collect_var(var)
        return var

    def flush_buffer(self):
        self.code.add_line('%s.extend([%s])'
                           % (self.result_var, ','.join(self.buffered)))
        self.buffered = []

    def strip_token(self, text, start, end):
        text = text.replace(start, '', 1)
        text = text.replace(end, '', 1)
        return text


class NoescapeText:

    def __init__(self, raw_text):
        self.raw_text = raw_text


html_escape_table = {
    '&': '&amp;',
    '"': '&quot;',
    '\'': '&apos;',
    '>': '&gt;',
    '<': '&lt;',
}


def html_escape(text):
    return ''.join(html_escape_table.get(c, c) for c in text)


def escape(text):
    if isinstance(text, NoescapeText):
        return to_text(text.raw_text)
    else:
        text = to_text(text)
        return html_escape(text)


def noescape(text):
    return NoescapeText(text)
