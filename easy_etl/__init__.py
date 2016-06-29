import os

from sqlalchemy.exc import OperationalError, ProgrammingError
from tqdm import tqdm


class ETLProcess(object):
    def __init__(self, read_db, write_db, write_table_name):
        self.read_db = read_db
        self.write_db = write_db
        self.write_table_name = write_table_name
        self.transform_pipeline = TransformPipeline()
        self.links = []
        self._middleware = []
        self._ignored = []

    def __unicode__(self):
        return "ETLProcess: (table: {})".format(self.write_table_name)

    def extract(self, sql, write_pk_field=None):
        if sql.endswith('.sql'):
            with open(sql, 'r') as f:
                sql = f.read()
        if write_pk_field:
            self.extract_method = (self._format_sql, (sql, write_pk_field))
        else:
            self.extract_method = (self.read_db.query, (sql,))

    def transform(self, *fields):
        self.transform_pipeline.fields = fields
        return self.transform_pipeline

    def load(self, upsert_fields=None, safe=False):
        method, args = self.extract_method
        if os.getenv("VERBOSE"):
            print("Querying data...")
        results = self._apply_middleware(method(*args))
        if results:
            if os.getenv("VERBOSE"):
                print("Loading {}".format(self.write_table_name))
                results = tqdm(results)

            table = self.write_db[self.write_table_name]
            self._write_rows(table, results, upsert_fields, safe)

    def extract_override(self, f):
        self.extract_method = (f, tuple())

    def link(self, field, table_name, child_field, name=None):
        self.links.append((field, table_name, child_field, {"name": name}))

    def link_closest(self, field, table_name, child_field, name=None,
                     method=">="):
        self.links.append((field, table_name, child_field,
                           {'closest_method': method, 'name': name}))

    def middleware(self, f):
        self._middleware.append(f)

    def ignore(self, *args):
        self._ignored += args

    def _apply_middleware(self, results):
        for middleware in self._middleware:
            results = middleware(results)
        return results

    def _format_sql(self, sql, write_pk_field):
        last_pk = 0
        try:
            rows = self.write_db.query("SELECT MAX({}) max FROM {};".format(
                write_pk_field, self.write_table_name
            ))
        except (OperationalError, ProgrammingError):
            pass
        else:
            last_pk = next(rows)['max'] or last_pk
        return self.read_db.query(sql.format(last_pk))

    def _write_rows(self, table, rows, upsert_fields, safe=False):
        dropped = False
        for row in rows:
            row_data = self.transform_pipeline.transform(row)
            row_data = self._make_links(row_data)
            row_data = self._remove_ignored(row_data)
            if upsert_fields:
                table.upsert(row_data, upsert_fields)
            else:
                table.insert(row_data)
            if not dropped and not safe:
                self._drop_old_columns(table, row_data.keys())
                dropped = True

    def _remove_ignored(self, row):
        for field in self._ignored:
            row.pop(field)
        return row

    def _drop_old_columns(self, table, current_columns):
        current_columns += ['id']
        for column in table.columns:
            if column not in current_columns:
                table.drop_column(column)

    def _make_links(self, row_data):
        for field, table_name, child_field, options in self.links:
            closest_method = options.get('closest_method')
            if closest_method:
                query = "SELECT id FROM {0} WHERE {1} " + closest_method + " \
                    {2} ORDER BY {1}"
            else:
                query = "SELECT id FROM {0} WHERE {1} = {2}"
            res = self.write_db.query(query.format(
                table_name, child_field, row_data[field]
            ))
            try:
                id = next(res)['id']
            except StopIteration:
                id = None
            row_data[options.get("name", field)] = id
        return row_data


def default(data):
    def inner(default_value):
        return data or default_value
    return inner


def func(data):
    def inner(f):
        return f(data)
    return inner


class TransformPipeline(object):
    fields = []
    pipeline = {}
    builtin_methods = {
        'default': default,
        'func': func,
    }

    def __unicode__(self):
        return "TransformPipeline (fields: {})".format(self.fields)

    def __getattr__(self, method, *args, **kwargs):
        def inner(*args, **kwargs):
            for field in self.fields:
                if field not in self.pipeline:
                    self.pipeline[field] = []
                self.pipeline[field].append((method, args, kwargs))
            return self
        return inner

    def transform(self, row):
        for field, methods in self.pipeline.items():
            row[field] = self._update(row[field], methods)
        return row

    def _update(self, data, methods):
        for method, args, kwargs in methods:
            try:
                f = getattr(data, method)
            except AttributeError:
                f = self.builtin_methods[method](data)
            data = f(*args, **kwargs)
        return data