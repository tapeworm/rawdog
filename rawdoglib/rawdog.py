# rawdog: RSS aggregator without delusions of grandeur.
# Copyright 2003, 2004, 2005, 2006, 2007, 2008, 2009, 2010, 2012, 2013 Adam Sampson <ats@offog.org>
#
# rawdog is free software; you can redistribute and/or modify it
# under the terms of that license as published by the Free Software
# Foundation; either version 2 of the License, or (at your option)
# any later version.
#
# rawdog is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with rawdog; see the file COPYING. If not, write to the Free
# Software Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA 02110-1301, USA, or see http://www.gnu.org/.

VERSION = "2.15rc1"
STATE_VERSION = 2
import feedparser, plugins
from persister import Persistable, Persister
import os, time, getopt, sys, re, cgi, socket, urllib2, calendar
import string, locale
from StringIO import StringIO
import types
import threading
import hashlib

try:
	import tidylib
except:
	tidylib = None

try:
	import mx.Tidy as mxtidy
except:
	mxtidy = None

try:
	import feedfinder
except:
	feedfinder = None

system_encoding = None
def get_system_encoding():
	"""Get the system encoding."""
	return system_encoding

def safe_ftime(format, t):
	"""Format a time value into a string in the current locale (as
	time.strftime), but encode the result as ASCII HTML."""
	u = unicode(time.strftime(format, t), get_system_encoding())
	return encode_references(u)

def format_time(secs, config):
	"""Format a time and date nicely."""
	t = time.localtime(secs)
	format = config["datetimeformat"]
	if format is None:
		format = config["timeformat"] + ", " + config["dayformat"]
	return safe_ftime(format, t)

high_char_re = re.compile(r'[^\000-\177]')
def encode_references(s):
	"""Encode characters in a Unicode string using HTML references."""
	def encode(m):
		return "&#" + str(ord(m.group(0))) + ";"
	return high_char_re.sub(encode, s)

# This list of block-level elements came from the HTML 4.01 specification.
block_level_re = re.compile(r'^\s*<(p|h1|h2|h3|h4|h5|h6|ul|ol|pre|dl|div|noscript|blockquote|form|hr|table|fieldset|address)[^a-z]', re.I)
def sanitise_html(html, baseurl, inline, config):
	"""Attempt to turn arbitrary feed-provided HTML into something
	suitable for safe inclusion into the rawdog output. The inline
	parameter says whether to expect a fragment of inline text, or a
	sequence of block-level elements."""
	if html is None:
		return None

	html = encode_references(html)
	type = "text/html"

	# sgmllib handles "<br/>/" as a SHORTTAG; this workaround from
	# feedparser.
	html = re.sub(r'(\S)/>', r'\1 />', html)

	# sgmllib is fragile with broken processing instructions (e.g.
	# "<!doctype html!>"); just remove them all.
	html = re.sub(r'<![^>]*>', '', html)

	html = feedparser._resolveRelativeURIs(html, baseurl, "UTF-8", type)
	p = feedparser._HTMLSanitizer("UTF-8", type)
	p.feed(html)
	html = p.output()

	if not inline and config["blocklevelhtml"]:
		# If we're after some block-level HTML and the HTML doesn't
		# start with a block-level element, then insert a <p> tag
		# before it. This still fails when the HTML contains text, then
		# a block-level element, then more text, but it's better than
		# nothing.
		if block_level_re.match(html) is None:
			html = "<p>" + html

	if config["tidyhtml"]:
		args = {"numeric_entities": 1,
		        "output_html": 1,
		        "output_xhtml": 0,
		        "output_xml": 0,
		        "wrap": 0}
		plugins.call_hook("mxtidy_args", config, args, baseurl, inline)
		plugins.call_hook("tidy_args", config, args, baseurl, inline)
		if tidylib is not None:
			# Disable PyTidyLib's somewhat unhelpful defaults.
			tidylib.BASE_OPTIONS = {}
			output = tidylib.tidy_document(html, args)[0]
		elif mxtidy is not None:
			output = mxtidy.tidy(html, None, None, **args)[2]
		else:
			# No Tidy bindings installed -- do nothing.
			output = "<body>" + html + "</body>"
		html = output[output.find("<body>") + 6
		              : output.rfind("</body>")].strip()

	html = html.decode("UTF-8")
	box = plugins.Box(html)
	plugins.call_hook("clean_html", config, box, baseurl, inline)
	return box.value

def select_detail(details):
	"""Pick the preferred type of detail from a list of details. (If the
	argument isn't a list, treat it as a list of one.)"""
	types = {"text/html": 30,
	         "application/xhtml+xml": 20,
	         "text/plain": 10}

	if details is None:
		return None
	if type(details) is not list:
		details = [details]

	ds = []
	for detail in details:
		ctype = detail.get("type", None)
		if ctype is None:
			continue
		if types.has_key(ctype):
			score = types[ctype]
		else:
			score = 0
		if detail["value"] != "":
			ds.append((score, detail))
	ds.sort()

	if len(ds) == 0:
		return None
	else:
		return ds[-1][1]

def detail_to_html(details, inline, config, force_preformatted=False):
	"""Convert a detail hash or list of detail hashes as returned by
	feedparser into HTML."""
	detail = select_detail(details)
	if detail is None:
		return None

	if force_preformatted:
		html = "<pre>" + cgi.escape(detail["value"]) + "</pre>"
	elif detail["type"] == "text/plain":
		html = cgi.escape(detail["value"])
	else:
		html = detail["value"]

	return sanitise_html(html, detail["base"], inline, config)

def author_to_html(entry, feedurl, config):
	"""Convert feedparser author information to HTML."""
	author_detail = entry.get("author_detail")

	if author_detail is not None and author_detail.has_key("name"):
		name = author_detail["name"]
	else:
		name = entry.get("author")

	url = None
	fallback = "author"
	if author_detail is not None:
		if author_detail.has_key("url"):
			url = author_detail["url"]
		elif author_detail.has_key("email") and author_detail["email"] is not None:
			url = "mailto:" + author_detail["email"]
		if author_detail.has_key("email") and author_detail["email"] is not None:
			fallback = author_detail["email"]
		elif author_detail.has_key("url") and author_detail["url"] is not None:
			fallback = author_detail["url"]

	if name == "":
		name = fallback

	if url is None:
		html = name
	else:
		html = "<a href=\"" + cgi.escape(url) + "\">" + cgi.escape(name) + "</a>"

	# We shouldn't need a base URL here anyway.
	return sanitise_html(html, feedurl, True, config)

def string_to_html(s, config):
	"""Convert a string to HTML."""
	return sanitise_html(cgi.escape(s), "", True, config)

template_re = re.compile(r'(__[^_].*?__)')
def fill_template(template, bits):
	"""Expand a template, replacing __x__ with bits["x"], and only
	including sections bracketed by __if_x__ .. [__else__ ..]
	__endif__ if bits["x"] is not "". If not bits.has_key("x"),
	__x__ expands to ""."""
	result = plugins.Box()
	plugins.call_hook("fill_template", template, bits, result)
	if result.value is not None:
		return result.value

	encoding = get_system_encoding()

	f = StringIO()
	if_stack = []
	def write(s):
		if not False in if_stack:
			f.write(s)
	for part in template_re.split(template):
		if part.startswith("__") and part.endswith("__"):
			key = part[2:-2]
			if key.startswith("if_"):
				k = key[3:]
				if_stack.append(bits.has_key(k) and bits[k] != "")
			elif key == "endif":
				if if_stack != []:
					if_stack.pop()
			elif key == "else":
				if if_stack != []:
					if_stack.append(not if_stack.pop())
			elif bits.has_key(key):
				if type(bits[key]) == types.UnicodeType:
					write(bits[key].encode(encoding))
				else:
					write(bits[key])
		else:
			write(part)
	v = f.getvalue()
	f.close()
	return v

file_cache = {}
def load_file(name):
	"""Read the contents of a file, caching the result so we don't have to
	read the file multiple times."""
	if not file_cache.has_key(name):
		f = open(name)
		file_cache[name] = f.read()
		f.close()
	return file_cache[name]

def write_ascii(f, s, config):
	"""Write the string s, which should only contain ASCII characters, to
	file f; if it isn't encodable in ASCII, then print a warning message
	and write UTF-8."""
	try:
		f.write(s)
	except UnicodeEncodeError, e:
		config.bug("Error encoding output as ASCII; UTF-8 has been written instead.\n", e)
		f.write(s.encode("UTF-8"))

def short_hash(s):
	"""Return a human-manipulatable 'short hash' of a string."""
	return hashlib.sha1(s).hexdigest()[-8:]

def ensure_unicode(value, encoding):
	"""Convert a structure returned by feedparser into an equivalent where
	all strings are represented as fully-decoded unicode objects."""

	if isinstance(value, str):
		try:
			return value.decode(encoding)
		except:
			# If the encoding's invalid, at least preserve
			# the byte stream.
			return value.decode("ISO-8859-1")
	elif isinstance(value, unicode) and type(value) is not unicode:
		# This is a subclass of unicode (e.g.  BeautifulSoup's
		# NavigableString, which is unpickleable in some versions of
		# the library), so force it to be a real unicode object.
		return unicode(value)
	elif isinstance(value, dict):
		d = {}
		for (k, v) in value.items():
			d[k] = ensure_unicode(v, encoding)
		return d
	elif isinstance(value, list):
		return [ensure_unicode(v, encoding) for v in value]
	else:
		return value

non_alphanumeric_re = re.compile(r'<[^>]*>|\&[^\;]*\;|[^a-z0-9]')
class Feed:
	"""An RSS feed."""

	def __init__(self, url):
		self.url = url
		self.period = 30 * 60
		self.args = {}
		self.etag = None
		self.modified = None
		self.last_update = 0
		self.feed_info = {}

	def needs_update(self, now):
		"""Return True if it's time to update this feed, or False if
		its update period has not yet elapsed."""
		return ((now - self.last_update) >= self.period)

	def get_state_filename(self):
		return "feeds/%s.state" % (short_hash(self.url),)

	def fetch(self, rawdog, config):
		"""Fetch the current set of articles from the feed."""

		handlers = []

		proxies = {}
		for key, arg in self.args.items():
			if key.endswith("_proxy"):
				proxies[key[:-6]] = arg
		if len(proxies) != 0:
			handlers.append(urllib2.ProxyHandler(proxies))

		if self.args.has_key("proxyuser") and self.args.has_key("proxypassword"):
			mgr = DummyPasswordMgr((self.args["proxyuser"], self.args["proxypassword"]))
			handlers.append(urllib2.ProxyBasicAuthHandler(mgr))

		plugins.call_hook("add_urllib2_handlers", rawdog, config, self, handlers)

		auth_creds = None
		if self.args.has_key("user") and self.args.has_key("password"):
			auth_creds = (self.args["user"], self.args["password"])

		use_im = True
		if self.get_keepmin(config) == 0 or config["currentonly"]:
			use_im = False

		try:
			return feedparser.parse(self.url,
				etag = self.etag,
				modified = self.modified,
				agent = "rawdog/" + VERSION,
				handlers = handlers,
				auth_creds = auth_creds,
				use_im = use_im)
		except Exception, e:
			return {
				"rawdog_exception": e,
				"rawdog_traceback": sys.exc_info()[2],
				}

	def update(self, rawdog, now, config, articles, p):
		"""Add new articles from a feed to the collection.
		Returns True if any articles were read, False otherwise."""

		status = p.get("status")
		self.last_update = now

		error = None
		non_fatal = False
		old_url = self.url
		if "rawdog_exception" in p:
			error = "Error fetching or parsing feed: %s" % str(p["rawdog_exception"])
			if config["showtracebacks"]:
				from traceback import format_tb
				error += "\n" + "".join(format_tb(p["rawdog_traceback"]))
		elif status is None and len(p["feed"]) == 0:
			if config["ignoretimeouts"]:
				return False
			else:
				error = "Timeout while reading feed."
		elif status is None:
			# Fetched by some protocol that doesn't have status.
			pass
		elif status == 301:
			# Permanent redirect. The feed URL needs changing.

			error = "New URL:     " + p["url"] + "\n"
			error += "The feed has moved permanently to a new URL.\n"
			if config["changeconfig"]:
				rawdog.change_feed_url(self.url, p["url"], config)
				error += "The config file has been updated automatically."
			else:
				error += "You should update its entry in your config file."
			non_fatal = True
		elif status in [403, 410]:
			# The feed is disallowed or gone. The feed should be unsubscribed.
			error = "The feed has gone.\n"
			error += "You should remove it from your config file."
		elif status / 100 in [4, 5]:
			# Some sort of client or server error. The feed may need unsubscribing.
			error = "The feed returned an error.\n"
			error += "If this condition persists, you should remove it from your config file."

		plugins.call_hook("feed_fetched", rawdog, config, self, p, error, non_fatal)

		if error is not None:
			print >>sys.stderr, "Feed:        " + old_url
			if status is not None:
				print >>sys.stderr, "HTTP Status: " + str(status)
			print >>sys.stderr, error
			print >>sys.stderr
			if not non_fatal:
				return False

		p = ensure_unicode(p, p.get("encoding") or "UTF-8")

		# In the event that the feed hasn't changed, then both channel
		# and feed will be empty. In this case we return False so that
		# we know not to expire articles that came from this feed.
		if len(p["entries"]) == 0:
			return False

		self.etag = p.get("etag")
		self.modified = p.get("modified")

		self.feed_info = p["feed"]
		feed = self.url

		article_ids = {}
		if config["useids"]:
			# Find IDs for existing articles.
			for (hash, a) in articles.items():
				id = a.entry_info.get("id")
				if a.feed == feed and id is not None:
					article_ids[id] = a

		seen = {}
		sequence = 0
		for entry_info in p["entries"]:
			article = Article(feed, entry_info, now, sequence)
			ignore = plugins.Box(False)
			plugins.call_hook("article_seen", rawdog, config, article, ignore)
			if ignore.value:
				continue
			seen[article.hash] = True
			sequence += 1

			id = entry_info.get("id")
			if id in article_ids:
				existing_article = article_ids[id]
			elif article.hash in articles:
				existing_article = articles[article.hash]
			else:
				existing_article = None

			if existing_article is not None:
				existing_article.update_from(article, now)
				plugins.call_hook("article_updated", rawdog, config, existing_article, now)
			else:
				articles[article.hash] = article
				plugins.call_hook("article_added", rawdog, config, article, now)

		if config["currentonly"]:
			for (hash, a) in articles.items():
				if a.feed == feed and not seen.has_key(hash):
					del articles[hash]

		return True

	def get_html_name(self, config):
		if self.feed_info.has_key("title_detail"):
			r = detail_to_html(self.feed_info["title_detail"], True, config)
		elif self.feed_info.has_key("link"):
			r = string_to_html(self.feed_info["link"], config)
		else:
			r = string_to_html(self.url, config)
		if r is None:
			r = ""
		return r

	def get_html_link(self, config):
		s = self.get_html_name(config)
		if self.feed_info.has_key("link"):
			return '<a href="' + string_to_html(self.feed_info["link"], config) + '">' + s + '</a>'
		else:
			return s

	def get_id(self, config):
		if self.args.has_key("id"):
			return self.args["id"]
		else:
			r = self.get_html_name(config).lower()
			return non_alphanumeric_re.sub('', r)

	def get_keepmin(self, config):
		try:
			return int(self.args["keepmin"])
		except:
			return config["keepmin"]

class Article:
	"""An article retrieved from an RSS feed."""

	def __init__(self, feed, entry_info, now, sequence):
		self.feed = feed
		self.entry_info = entry_info
		self.sequence = sequence

		self.date = None
		parsed = entry_info.get("updated_parsed")
		if parsed is None:
			parsed = entry_info.get("published_parsed")
		if parsed is None:
			parsed = entry_info.get("created_parsed")
		if parsed is not None:
			try:
				self.date = calendar.timegm(parsed)
			except OverflowError:
				pass

		self.hash = self.compute_initial_hash()

		self.last_seen = now
		self.added = now

	def compute_initial_hash(self):
		"""Compute an initial unique hash for an article.
		The generated hash must be unique amongst all articles in the
		system (i.e. it can't just be the article ID, because that
		would collide if more than one feed included the same
		article)."""
		h = hashlib.sha1()
		def add_hash(s):
			h.update(s.encode("UTF-8"))

		add_hash(self.feed)
		entry_info = self.entry_info
		if entry_info.has_key("title_raw"):
			add_hash(entry_info["title_raw"])
		if entry_info.has_key("link"):
			add_hash(entry_info["link"])
		if entry_info.has_key("content"):
			for content in entry_info["content"]:
				add_hash(content["value_raw"])
		if entry_info.has_key("summary_detail"):
			add_hash(entry_info["summary_detail"]["value_raw"])

		return h.hexdigest()

	def update_from(self, new_article, now):
		"""Update this article's contents from a newer article that's
		been identified to be the same."""
		self.entry_info = new_article.entry_info
		self.sequence = new_article.sequence
		self.date = new_article.date
		self.last_seen = now

	def can_expire(self, now, config):
		return ((now - self.last_seen) > config["expireage"])

	def get_sort_date(self, config):
		if config["sortbyfeeddate"]:
			return self.date or self.added
		else:
			return self.added

class DayWriter:
	"""Utility class for writing day sections into a series of articles."""

	def __init__(self, file, config):
		self.lasttime = [-1, -1, -1, -1, -1]
		self.file = file
		self.counter = 0
		self.config = config

	def start_day(self, tm):
		print >>self.file, '<div class="day">'
		day = safe_ftime(self.config["dayformat"], tm)
		print >>self.file, '<h2>' + day + '</h2>'
		self.counter += 1

	def start_time(self, tm):
		print >>self.file, '<div class="time">'
		clock = safe_ftime(self.config["timeformat"], tm)
		print >>self.file, '<h3>' + clock + '</h3>'
		self.counter += 1

	def time(self, s):
		tm = time.localtime(s)
		if tm[:3] != self.lasttime[:3] and self.config["daysections"]:
			self.close(0)
			self.start_day(tm)
		if tm[:6] != self.lasttime[:6] and self.config["timesections"]:
			if self.config["daysections"]:
				self.close(1)
			else:
				self.close(0)
			self.start_time(tm)
		self.lasttime = tm

	def close(self, n=0):
		while self.counter > n:
			print >>self.file, "</div>"
			self.counter -= 1

def parse_time(value, default="m"):
	"""Parse a time period with optional units (s, m, h, d, w) into a time
	in seconds. If no unit is specified, use minutes by default; specify
	the default argument to change this. Raises ValueError if the format
	isn't recognised."""
	units = { "s" : 1, "m" : 60, "h" : 3600, "d" : 86400, "w" : 604800 }
	for unit, size in units.items():
		if value.endswith(unit):
			return int(value[:-len(unit)]) * size
	return int(value) * units[default]

def parse_bool(value):
	"""Parse a boolean value (0, 1, false or true). Raise ValueError if
	the value isn't recognised."""
	value = value.strip().lower()
	if value == "0" or value == "false":
		return False
	elif value == "1" or value == "true":
		return True
	else:
		raise ValueError("Bad boolean value: " + value)

def parse_list(value):
	"""Parse a list of keywords separated by whitespace."""
	return value.strip().split(None)

def parse_feed_args(argparams, arglines):
	"""Parse a list of feed arguments. Raise ConfigError if the syntax is invalid."""
	args = {}
	for p in argparams:
		ps = p.split("=", 1)
		if len(ps) != 2:
			raise ConfigError("Bad feed argument in config: " + p)
		args[ps[0]] = ps[1]
	for p in arglines:
		ps = p.split(None, 1)
		if len(ps) != 2:
			raise ConfigError("Bad argument line in config: " + p)
		args[ps[0]] = ps[1]
	if "maxage" in args:
		args["maxage"] = parse_time(args["maxage"])
	return args

class ConfigError(Exception): pass

class Config:
	"""The aggregator's configuration."""

	def __init__(self, locking):
		self.locking = locking
		self.files_loaded = []
		self.loglock = threading.Lock()
		self.reset()

	def reset(self):
		self.config = {
			"feedslist" : [],
			"feeddefaults" : {},
			"defines" : {},
			"outputfile" : "output.html",
			"maxarticles" : 200,
			"maxage" : 0,
			"expireage" : 24 * 60 * 60,
			"keepmin" : 0,
			"dayformat" : "%A, %d %B %Y",
			"timeformat" : "%I:%M %p",
			"datetimeformat" : None,
			"userefresh" : False,
			"showfeeds" : True,
			"timeout" : 30,
			"template" : "default",
			"itemtemplate" : "default",
			"verbose" : False,
			"ignoretimeouts" : False,
			"showtracebacks" : False,
			"daysections" : True,
			"timesections" : True,
			"blocklevelhtml" : True,
			"tidyhtml" : False,
			"sortbyfeeddate" : False,
			"currentonly" : False,
			"hideduplicates" : "",
			"newfeedperiod" : "3h",
			"changeconfig": False,
			"numthreads": 0,
			"splitstate": False,
			"useids": False,
			}

	def __getitem__(self, key): return self.config[key]
	def __setitem__(self, key, value): self.config[key] = value

	def reload(self):
		self.log("Reloading config files")
		self.reset()
		for filename in self.files_loaded:
			self.load(filename, False)

	def load(self, filename, explicitly_loaded=True):
		"""Load configuration from a config file."""
		if explicitly_loaded:
			self.files_loaded.append(filename)

		lines = []
		try:
			f = open(filename, "r")
			for line in f.xreadlines():
				stripped = line.decode(get_system_encoding()).strip()
				if stripped == "" or stripped[0] == "#":
					continue
				if line[0] in string.whitespace:
					if lines == []:
						raise ConfigError("First line in config cannot be an argument")
					lines[-1][1].append(stripped)
				else:
					lines.append((stripped, []))
			f.close()
		except IOError:
			raise ConfigError("Can't read config file: " + filename)

		for line, arglines in lines:
			try:
				self.load_line(line, arglines)
			except ValueError:
				raise ConfigError("Bad value in config: " + line)

	def load_line(self, line, arglines):
		"""Process a configuration directive."""

		l = line.split(None, 1)
		if len(l) == 1 and l[0] == "feeddefaults":
			l.append("")
		elif len(l) != 2:
			raise ConfigError("Bad line in config: " + line)

		handled_arglines = False
		if l[0] == "feed":
			l = l[1].split(None)
			if len(l) < 2:
				raise ConfigError("Bad line in config: " + line)
			self["feedslist"].append((l[1], parse_time(l[0]), parse_feed_args(l[2:], arglines)))
			handled_arglines = True
		elif l[0] == "feeddefaults":
			self["feeddefaults"] = parse_feed_args(l[1].split(None), arglines)
			handled_arglines = True
		elif l[0] == "define":
			l = l[1].split(None, 1)
			if len(l) != 2:
				raise ConfigError("Bad line in config: " + line)
			self["defines"][l[0]] = l[1]
		elif l[0] == "plugindirs":
			for dir in parse_list(l[1]):
				plugins.load_plugins(dir, self)
		elif l[0] == "outputfile":
			self["outputfile"] = l[1]
		elif l[0] == "maxarticles":
			self["maxarticles"] = int(l[1])
		elif l[0] == "maxage":
			self["maxage"] = parse_time(l[1])
		elif l[0] == "expireage":
			self["expireage"] = parse_time(l[1])
		elif l[0] == "keepmin":
			self["keepmin"] = int(l[1])
		elif l[0] == "dayformat":
			self["dayformat"] = l[1]
		elif l[0] == "timeformat":
			self["timeformat"] = l[1]
		elif l[0] == "datetimeformat":
			self["datetimeformat"] = l[1]
		elif l[0] == "userefresh":
			self["userefresh"] = parse_bool(l[1])
		elif l[0] == "showfeeds":
			self["showfeeds"] = parse_bool(l[1])
		elif l[0] == "timeout":
			self["timeout"] = parse_time(l[1], "s")
		elif l[0] == "template":
			self["template"] = l[1]
		elif l[0] == "itemtemplate":
			self["itemtemplate"] = l[1]
		elif l[0] == "verbose":
			self["verbose"] = parse_bool(l[1])
		elif l[0] == "ignoretimeouts":
			self["ignoretimeouts"] = parse_bool(l[1])
		elif l[0] == "showtracebacks":
			self["showtracebacks"] = parse_bool(l[1])
		elif l[0] == "daysections":
			self["daysections"] = parse_bool(l[1])
		elif l[0] == "timesections":
			self["timesections"] = parse_bool(l[1])
		elif l[0] == "blocklevelhtml":
			self["blocklevelhtml"] = parse_bool(l[1])
		elif l[0] == "tidyhtml":
			self["tidyhtml"] = parse_bool(l[1])
		elif l[0] == "sortbyfeeddate":
			self["sortbyfeeddate"] = parse_bool(l[1])
		elif l[0] == "currentonly":
			self["currentonly"] = parse_bool(l[1])
		elif l[0] == "hideduplicates":
			self["hideduplicates"] = parse_list(l[1])
		elif l[0] == "newfeedperiod":
			self["newfeedperiod"] = l[1]
		elif l[0] == "changeconfig":
			self["changeconfig"] = parse_bool(l[1])
		elif l[0] == "numthreads":
			self["numthreads"] = int(l[1])
		elif l[0] == "splitstate":
			self["splitstate"] = parse_bool(l[1])
		elif l[0] == "useids":
			self["useids"] = parse_bool(l[1])
		elif l[0] == "include":
			self.load(l[1], False)
		elif plugins.call_hook("config_option_arglines", self, l[0], l[1], arglines):
			handled_arglines = True
		elif plugins.call_hook("config_option", self, l[0], l[1]):
			pass
		else:
			raise ConfigError("Unknown config command: " + l[0])

		if arglines != [] and not handled_arglines:
			raise ConfigError("Bad argument lines in config after: " + line)

	def log(self, *args):
		"""If running in verbose mode, print a status message."""
		if self["verbose"]:
			self.loglock.acquire()
			print >>sys.stderr, "".join(map(str, args))
			self.loglock.release()

	def bug(self, *args):
		"""Report detection of a bug in rawdog."""
		print >>sys.stderr, "Internal error detected in rawdog:"
		print >>sys.stderr, "".join(map(str, args))
		print >>sys.stderr, "This could be caused by a bug in rawdog itself or in a plugin."
		print >>sys.stderr, "Please send this error message and your config file to the rawdog author."

def edit_file(filename, editfunc):
	"""Edit a file in place: for each line in the input file, call
	editfunc(line, outputfile), then rename the output file over the input
	file."""
	newname = "%s.new-%d" % (filename, os.getpid())
	oldfile = open(filename, "r")
	newfile = open(newname, "w")
	editfunc(oldfile, newfile)
	newfile.close()
	oldfile.close()
	os.rename(newname, filename)

class AddFeedEditor:
	def __init__(self, feedline):
		self.feedline = feedline
	def edit(self, inputfile, outputfile):
		d = inputfile.read()
		outputfile.write(d)
		if not d.endswith("\n"):
			outputfile.write("\n")
		outputfile.write(self.feedline)

def add_feed(filename, url, rawdog, config):
	"""Try to add a feed to the config file."""
	if feedfinder is None:
		feeds = [url]
	else:
		feeds = feedfinder.feeds(url)
	if feeds == []:
		print >>sys.stderr, "Cannot find any feeds in " + url
		return

	# Sort feeds into preference order: some sites provide feeds for
	# content and comments, or both Atom and RSS.
	part_scores = {"comment": -10,
	               "atom": 2}
	scored = []
	for feed in feeds:
		score = 0
		for p, s in part_scores.items():
			if feed.find(p) != -1:
				score += s
		scored.append((-score, feed))
	scored.sort()

	feed = scored[0][1]
	if feed in rawdog.feeds:
		print >>sys.stderr, "Feed " + feed + " is already in the config file"
		return

	print >>sys.stderr, "Adding feed " + feed
	feedline = "feed %s %s\n" % (config["newfeedperiod"], feed)
	edit_file(filename, AddFeedEditor(feedline).edit)

class ChangeFeedEditor:
	def __init__(self, oldurl, newurl):
		self.oldurl = oldurl
		self.newurl = newurl
	def edit(self, inputfile, outputfile):
		for line in inputfile.xreadlines():
			ls = line.strip().split(None)
			if len(ls) > 2 and ls[0] == "feed" and ls[2] == self.oldurl:
				line = line.replace(self.oldurl, self.newurl, 1)
			outputfile.write(line)

class RemoveFeedEditor:
	def __init__(self, url):
		self.url = url
	def edit(self, inputfile, outputfile):
		while 1:
			l = inputfile.readline()
			if l == "":
				break
			ls = l.strip().split(None)
			if len(ls) > 2 and ls[0] == "feed" and ls[2] == self.url:
				while 1:
					l = inputfile.readline()
					if l == "":
						break
					elif l[0] == "#":
						outputfile.write(l)
					elif l[0] not in string.whitespace:
						outputfile.write(l)
						break
			else:
				outputfile.write(l)

def remove_feed(filename, url, config):
	"""Try to remove a feed from the config file."""
	if url not in [f[0] for f in config["feedslist"]]:
		print >>sys.stderr, "Feed " + url + " is not in the config file"
	else:
		print >>sys.stderr, "Removing feed " + url
		edit_file(filename, RemoveFeedEditor(url).edit)

class FeedFetcher:
	"""Class that will handle fetching a set of feeds in parallel."""

	def __init__(self, rawdog, feedlist, config):
		self.rawdog = rawdog
		self.config = config
		self.lock = threading.Lock()
		self.jobs = {}
		for feed in feedlist:
			self.jobs[feed] = 1
		self.results = {}

	def worker(self, num):
		rawdog = self.rawdog
		config = self.config

		config.log("Thread ", num, " starting")
		while 1:
			self.lock.acquire()
			if self.jobs == {}:
				job = None
			else:
				job = self.jobs.keys()[0]
				del self.jobs[job]
			self.lock.release()
			if job is None:
				break

			config.log("Thread ", num, " fetching feed: ", job)
			feed = rawdog.feeds[job]
			plugins.call_hook("pre_update_feed", rawdog, config, feed)
			self.results[job] = feed.fetch(rawdog, config)
		config.log("Thread ", num, " done")

	def run(self, numworkers):
		self.config.log("Thread farm starting with ", len(self.jobs), " jobs")
		workers = []
		for i in range(numworkers):
			self.lock.acquire()
			isempty = (self.jobs == {})
			self.lock.release()
			if isempty:
				# No jobs left in the queue -- don't bother
				# starting any more workers.
				break

			t = threading.Thread(target=self.worker, args=(i,))
			t.start()
			workers.append(t)
		for worker in workers:
			worker.join()
		self.config.log("Thread farm finished with ", len(self.results), " results")
		return self.results

class FeedState(Persistable):
	"""The collection of articles in a feed."""

	def __init__(self):
		self.articles = {}

class Rawdog(Persistable):
	"""The aggregator itself."""

	def __init__(self):
		self.feeds = {}
		self.articles = {}
		self.plugin_storage = {}
		self.state_version = STATE_VERSION
		self.using_splitstate = None

	def get_plugin_storage(self, plugin):
		try:
			st = self.plugin_storage.setdefault(plugin, {})
		except AttributeError:
			# rawdog before 2.5 didn't have plugin storage.
			st = {}
			self.plugin_storage = {plugin: st}
		return st

	def check_state_version(self):
		"""Check the version of the state file."""
		try:
			version = self.state_version
		except AttributeError:
			# rawdog 1.x didn't keep track of this.
			version = 1
		return version == STATE_VERSION

	def change_feed_url(self, oldurl, newurl, config):
		"""Change the URL of a feed."""

		assert self.feeds.has_key(oldurl)
		if self.feeds.has_key(newurl):
			print >>sys.stderr, "Error: New feed URL is already subscribed; please remove the old one"
			print >>sys.stderr, "from the config file by hand."
			return

		edit_file("config", ChangeFeedEditor(oldurl, newurl).edit)

		feed = self.feeds[oldurl]
		old_state = feed.get_state_filename()
		feed.url = newurl
		del self.feeds[oldurl]
		self.feeds[newurl] = feed

		if config["splitstate"]:
			persister, feedstate = load_persisted(feed.get_state_filename(), FeedState, config)
			for article in feedstate.articles.values():
				article.feed = newurl
			feedstate.modified()
			save_persisted(persister, config)
			os.rename(old_state, feed.get_state_filename())
		else:
			for article in self.articles.values():
				if article.feed == oldurl:
					article.feed = newurl

		print >>sys.stderr, "Feed URL automatically changed."

	def list(self, config):
		"""List the configured feeds."""
		for url, feed in self.feeds.items():
			feed_info = feed.feed_info
			print url
			print "  ID:", feed.get_id(config)
			print "  Hash:", short_hash(url)
			print "  Title:", feed.get_html_name(config)
			print "  Link:", feed_info.get("link")

	def sync_from_config(self, config):
		"""Update rawdog's internal state to match the
		configuration."""

		try:
			u = self.using_splitstate
		except:
			# We were last run with a version of rawdog that didn't
			# have this variable -- so we must have a single state
			# file.
			u = False
		if u is None:
			self.using_splitstate = config["splitstate"]
		elif u != config["splitstate"]:
			if config["splitstate"]:
				config.log("Converting to split state files")
				for feed_hash, feed in self.feeds.items():
					persister, feedstate = load_persisted(feed.get_state_filename(), FeedState, config)
					feedstate.articles = {}
					for article_hash, article in self.articles.items():
						if article.feed == feed_hash:
							feedstate.articles[article_hash] = article
					feedstate.modified()
					save_persisted(persister, config)
				self.articles = {}
			else:
				config.log("Converting to single state file")
				self.articles = {}
				for feed_hash, feed in self.feeds.items():
					persister, feedstate = load_persisted(feed.get_state_filename(), FeedState, config)
					for article_hash, article in feedstate.articles.items():
						self.articles[article_hash] = article
					feedstate.articles = {}
					feedstate.modified()
					save_persisted(persister, config)
					os.unlink(feed.get_state_filename())
			self.modified()
			self.using_splitstate = config["splitstate"]

		seenfeeds = {}
		for (url, period, args) in config["feedslist"]:
			seenfeeds[url] = 1
			if not self.feeds.has_key(url):
				config.log("Adding new feed: ", url)
				self.feeds[url] = Feed(url)
				self.modified()
			feed = self.feeds[url]
			if feed.period != period:
				config.log("Changed feed period: ", url)
				feed.period = period
				self.modified()
			newargs = {}
			newargs.update(config["feeddefaults"])
			newargs.update(args)
			if feed.args != newargs:
				config.log("Changed feed options: ", url)
				feed.args = newargs
				self.modified()
		for url in self.feeds.keys():
			if not seenfeeds.has_key(url):
				config.log("Removing feed: ", url)
				if config["splitstate"]:
					try:
						os.unlink(self.feeds[url].get_state_filename())
					except OSError:
						pass
				else:
					for key, article in self.articles.items():
						if article.feed == url:
							del self.articles[key]
				del self.feeds[url]
				self.modified()

	def update(self, config, feedurl=None):
		"""Perform the update action: check feeds for new articles, and
		expire old ones."""
		config.log("Starting update")
		now = time.time()

		feedparser._FeedParserMixin.can_contain_relative_uris = ["url"]
		feedparser._FeedParserMixin.can_contain_dangerous_markup = []
		feedparser.BeautifulSoup = None
		socket.setdefaulttimeout(config["timeout"])

		if feedurl is None:
			update_feeds = [url for url in self.feeds.keys()
			                    if self.feeds[url].needs_update(now)]
		elif self.feeds.has_key(feedurl):
			update_feeds = [feedurl]
			self.feeds[feedurl].etag = None
			self.feeds[feedurl].modified = None
		else:
			print "No such feed: " + feedurl
			update_feeds = []

		numfeeds = len(update_feeds)
		config.log("Will update ", numfeeds, " feeds")

		if config["numthreads"] > 0:
			fetcher = FeedFetcher(self, update_feeds, config)
			prefetched = fetcher.run(config["numthreads"])
		else:
			prefetched = {}

		seen_some_items = {}
		def do_expiry(articles):
			feedcounts = {}
			for key, article in articles.items():
				url = article.feed
				feedcounts[url] = feedcounts.get(url, 0) + 1

			expiry_list = []
			feedcounts = {}
			for key, article in articles.items():
				url = article.feed
				feedcounts[url] = feedcounts.get(url, 0) + 1
				expiry_list.append((article.added, article.sequence, key, article))
			expiry_list.sort()

			count = 0
			for date, seq, key, article in expiry_list:
				url = article.feed
				if url not in self.feeds:
					config.log("Expired article for nonexistent feed: ", url)
					count += 1
					del articles[key]
					continue
				if (seen_some_items.has_key(url)
				    and self.feeds.has_key(url)
				    and article.can_expire(now, config)
				    and feedcounts[url] > self.feeds[url].get_keepmin(config)):
					plugins.call_hook("article_expired", self, config, article, now)
					count += 1
					feedcounts[url] -= 1
					del articles[key]
			config.log("Expired ", count, " articles, leaving ", len(articles))

		count = 0
		for url in update_feeds:
			count += 1
			config.log("Updating feed ", count, " of " , numfeeds, ": ", url)
			feed = self.feeds[url]

			if config["splitstate"]:
				persister, feedstate = load_persisted(feed.get_state_filename(), FeedState, config)
				articles = feedstate.articles
			else:
				articles = self.articles

			if url in prefetched:
				content = prefetched[url]
			else:
				plugins.call_hook("pre_update_feed", self, config, feed)
				content = feed.fetch(self, config)
			plugins.call_hook("mid_update_feed", self, config, feed, content)
			rc = feed.update(self, now, config, articles, content)
			url = feed.url
			plugins.call_hook("post_update_feed", self, config, feed, rc)
			if rc:
				seen_some_items[url] = 1
				if config["splitstate"]:
					feedstate.modified()

			if config["splitstate"]:
				do_expiry(articles)
				save_persisted(persister, config)

		if config["splitstate"]:
			self.articles = {}
		else:
			do_expiry(self.articles)

		self.modified()
		config.log("Finished update")

	def get_template(self, config):
		"""Get the main template."""
		if config["template"] != "default":
			return load_file(config["template"])

		template = """<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN"
   "http://www.w3.org/TR/html4/strict.dtd">
<html lang="en">
<head>
    <meta http-equiv="Content-Type" content="text/html; charset=ISO-8859-1">
    <meta name="robots" content="noindex,nofollow,noarchive">
"""
		if config["userefresh"]:
			template += """__refresh__
"""
		template += """    <link rel="stylesheet" href="style.css" type="text/css">
    <title>rawdog</title>
</head>
<body id="rawdog">
<div id="header">
<h1>rawdog</h1>
</div>
<div id="items">
__items__
</div>
"""
		if config["showfeeds"]:
			template += """<h2 id="feedstatsheader">Feeds</h2>
<div id="feedstats">
__feeds__
</div>
"""
		template += """<div id="footer">
<p id="aboutrawdog">Generated by
<a href="http://offog.org/code/rawdog.html">rawdog</a>
version __version__
by <a href="mailto:ats@offog.org">Adam Sampson</a>.</p>
</div>
</body>
</html>
"""
		return template

	def get_itemtemplate(self, config):
		"""Get the item template."""
		if config["itemtemplate"] != "default":
			return load_file(config["itemtemplate"])

		template = """<div class="item feed-__feed_hash__ feed-__feed_id__" id="item-__hash__">
<p class="itemheader">
<span class="itemtitle">__title__</span>
<span class="itemfrom">[__feed_title__]</span>
</p>
__if_description__<div class="itemdescription">
__description__
</div>__endif__
</div>

"""
		return template

	def show_template(self, config):
		"""Show the configured main template."""
		print self.get_template(config)

	def show_itemtemplate(self, config):
		"""Show the configured item template."""
		print self.get_itemtemplate(config)

	def write_article(self, f, article, config):
		"""Write an article to the given file."""
		feed = self.feeds[article.feed]
		feed_info = feed.feed_info
		entry_info = article.entry_info

		link = entry_info.get("link")
		if link == "":
			link = None

		guid = entry_info.get("id")
		if guid == "":
			guid = None

		itembits = {}
		for name, value in feed.args.items():
			if name.startswith("define_"):
				itembits[name[7:]] = sanitise_html(value, "", True, config)

		title = detail_to_html(entry_info.get("title_detail"), True, config)

		key = None
		for k in ["content", "summary_detail"]:
			if entry_info.has_key(k):
				key = k
				break
		if key is None:
			description = None
		else:
			force_preformatted = feed.args.has_key("format") and (feed.args["format"] == "text")
			description = detail_to_html(entry_info[key], False, config, force_preformatted)

		date = article.date
		if title is None:
			if link is None:
				title = "Article"
			else:
				title = "Link"

		itembits["title_no_link"] = title
		if link is not None:
			itembits["url"] = string_to_html(link, config)
		else:
			itembits["url"] = ""
		if guid is not None:
			itembits["guid"] = string_to_html(guid, config)
		else:
			itembits["guid"] = ""
		if link is None:
			itembits["title"] = title
		else:
			itembits["title"] = '<a href="' + string_to_html(link, config) + '">' + title + '</a>'

		itembits["feed_title_no_link"] = detail_to_html(feed_info.get("title_detail"), True, config)
		itembits["feed_title"] = feed.get_html_link(config)
		itembits["feed_url"] = string_to_html(feed.url, config)
		itembits["feed_hash"] = short_hash(feed.url)
		itembits["feed_id"] = feed.get_id(config)
		itembits["hash"] = short_hash(article.hash)

		if description is not None:
			itembits["description"] = description
		else:
			itembits["description"] = ""

		author = author_to_html(entry_info, feed.url, config)
		if author is not None:
			itembits["author"] = author
		else:
			itembits["author"] = ""

		itembits["added"] = format_time(article.added, config)
		if date is not None:
			itembits["date"] = format_time(date, config)
		else:
			itembits["date"] = ""

		plugins.call_hook("output_item_bits", self, config, feed, article, itembits)
		itemtemplate = self.get_itemtemplate(config)
		f.write(fill_template(itemtemplate, itembits))

	def write_remove_dups(self, articles, config, now):
		"""Filter the list of articles to remove articles that are too
		old or are duplicates."""
		kept_articles = []
		seen_links = {}
		seen_guids = {}
		dup_count = 0
		for article in articles:
			feed = self.feeds[article.feed]
			age = now - article.added

			maxage = config["maxage"]
			if "maxage" in feed.args:
				maxage = feed.args["maxage"]
			if maxage != 0 and age > maxage:
				continue

			entry_info = article.entry_info

			link = entry_info.get("link")
			if link == "":
				link = None

			guid = entry_info.get("id")
			if guid == "":
				guid = None

			if feed.args.get("allowduplicates") != "true":
				is_dup = False
				for key in config["hideduplicates"]:
					if key == "id" and guid is not None:
						if seen_guids.has_key(guid):
							is_dup = True
						seen_guids[guid] = 1
						break
					elif key == "link" and link is not None:
						if seen_links.has_key(link):
							is_dup = True
						seen_links[link] = 1
						break
				if is_dup:
					dup_count += 1
					continue

			kept_articles.append(article)
		return (kept_articles, dup_count)

	def get_main_template_bits(self, config):
		"""Get the bits that are used in the default main template,
		with the exception of items and num_items."""
		bits = { "version" : VERSION }
		bits.update(config["defines"])

		refresh = config["expireage"]
		for feed in self.feeds.values():
			if feed.period < refresh: refresh = feed.period

		bits["refresh"] = """<meta http-equiv="Refresh" """ + 'content="' + str(refresh) + '"' + """>"""

		f = StringIO()
		print >>f, """<table id="feeds">
<tr id="feedsheader">
<th>Feed</th><th>RSS</th><th>Last fetched</th><th>Next fetched after</th>
</tr>"""
		feeds = [(feed.get_html_name(config).lower(), feed)
		         for feed in self.feeds.values()]
		feeds.sort()
		for (key, feed) in feeds:
			print >>f, '<tr class="feedsrow">'
			print >>f, '<td>' + feed.get_html_link(config) + '</td>'
			print >>f, '<td><a class="xmlbutton" href="' + cgi.escape(feed.url) + '">XML</a></td>'
			print >>f, '<td>' + format_time(feed.last_update, config) + '</td>'
			print >>f, '<td>' + format_time(feed.last_update + feed.period, config) + '</td>'
			print >>f, '</tr>'
		print >>f, """</table>"""
		bits["feeds"] = f.getvalue()
		f.close()
		bits["num_feeds"] = str(len(feeds))

		return bits

	def write_output_file(self, articles, article_dates, config):
		"""Write a regular rawdog HTML output file."""
		f = StringIO()
		dw = DayWriter(f, config)
		plugins.call_hook("output_items_begin", self, config, f)

		for article in articles:
			if not plugins.call_hook("output_items_heading", self, config, f, article, article_dates[article]):
				dw.time(article_dates[article])

			self.write_article(f, article, config)

		dw.close()
		plugins.call_hook("output_items_end", self, config, f)

		bits = self.get_main_template_bits(config)
		bits["items"] = f.getvalue()
		f.close()
		bits["num_items"] = str(len(articles))
		plugins.call_hook("output_bits", self, config, bits)
		s = fill_template(self.get_template(config), bits)
		outputfile = config["outputfile"]
		if outputfile == "-":
			write_ascii(sys.stdout, s, config)
		else:
			config.log("Writing output file: ", outputfile)
			f = open(outputfile + ".new", "w")
			write_ascii(f, s, config)
			f.close()
			os.rename(outputfile + ".new", outputfile)

	def write(self, config):
		"""Perform the write action: write articles to the output
		file."""
		config.log("Starting write")
		now = time.time()

		def list_articles(articles):
			return [(-a.get_sort_date(config), a.feed, a.sequence, a.hash) for a in articles.values()]
		if config["splitstate"]:
			article_list = []
			for feed in self.feeds.values():
				persister, feedstate = load_persisted(feed.get_state_filename(), FeedState, config)
				article_list += list_articles(feedstate.articles)
				save_persisted(persister, config)
		else:
			article_list = list_articles(self.articles)
		numarticles = len(article_list)

		if not plugins.call_hook("output_sort_articles", self, config, article_list):
			article_list.sort()

		if config["maxarticles"] != 0:
			article_list = article_list[:config["maxarticles"]]

		if config["splitstate"]:
			wanted = {}
			for (date, feed_url, seq, hash) in article_list:
				if not feed_url in self.feeds:
					# This can happen if you've managed to
					# kill rawdog between it updating a
					# split state file and the main state
					# -- so just ignore the article and
					# it'll expire eventually.
					continue
				wanted.setdefault(feed_url, []).append(hash)

			found = {}
			for (feed_url, article_hashes) in wanted.items():
				feed = self.feeds[feed_url]
				persister, feedstate = load_persisted(feed.get_state_filename(), FeedState, config)
				for hash in article_hashes:
					found[hash] = feedstate.articles[hash]
				save_persisted(persister, config)
		else:
			found = self.articles

		articles = []
		article_dates = {}
		for (date, feed, seq, hash) in article_list:
			a = found.get(hash)
			if a is not None:
				articles.append(a)
				article_dates[a] = -date

		plugins.call_hook("output_write", self, config, articles)

		if not plugins.call_hook("output_sorted_filter", self, config, articles):
			(articles, dup_count) = self.write_remove_dups(articles, config, now)
		else:
			dup_count = 0

		config.log("Selected ", len(articles), " of ", numarticles, " articles to write; ignored ", dup_count, " duplicates")

		if not plugins.call_hook("output_write_files", self, config, articles, article_dates):
			self.write_output_file(articles, article_dates, config)

		config.log("Finished write")

def usage():
	"""Display usage information."""
	print """rawdog, version """ + VERSION + """
Usage: rawdog [OPTION]...

General options (use only once):
-d|--dir DIR                 Use DIR instead of ~/.rawdog
-v, --verbose                Print more detailed status information
-N, --no-locking             Do not lock the state file
-W, --no-lock-wait           Exit silently if state file is locked

Actions (performed in order given):
-u, --update                 Fetch data from feeds and store it
-l, --list                   List feeds known at time of last update
-w, --write                  Write out HTML output
-f|--update-feed URL         Force an update on the single feed URL
-c|--config FILE             Read additional config file FILE
-t, --show-template          Print the template currently in use
-T, --show-itemtemplate      Print the item template currently in use
-a|--add URL                 Try to find a feed associated with URL and
                             add it to the config file
-r|--remove URL              Remove feed URL from the config file

Special actions (all other options are ignored if one of these is specified):
--help                       Display this help and exit

Report bugs to <ats@offog.org>."""

def load_persisted(fn, klass, config, no_block=False):
	"""Attempt to load a persisted object. Returns the persister and the
	object."""
	config.log("Loading state file: ", fn)
	persister = Persister(fn, klass, config.locking)
	try:
		obj = persister.load(no_block=no_block)
	except KeyboardInterrupt:
		sys.exit(1)
	except:
		print "An error occurred while reading state from " + os.getcwd() + "/" + fn + "."
		print "This usually means the file is corrupt, and removing it will fix the problem."
		sys.exit(1)
	return (persister, obj)

def save_persisted(persister, config):
	if persister.object.is_modified():
		config.log("Saving state file: ", persister.filename)
	persister.save()

def main(argv):
	"""The command-line interface to the aggregator."""

	locale.setlocale(locale.LC_ALL, "")

	# This is quite expensive and not threadsafe, so we do it on
	# startup and cache the result.
	global system_encoding
	system_encoding = locale.getpreferredencoding()

	try:
		(optlist, args) = getopt.getopt(argv, "ulwf:c:tTd:va:r:NW", ["update", "list", "write", "update-feed=", "help", "config=", "show-template", "dir=", "show-itemtemplate", "verbose", "add=", "remove=", "no-locking", "no-lock-wait"])
	except getopt.GetoptError, s:
		print s
		usage()
		return 1

	if len(args) != 0:
		usage()
		return 1

	if "HOME" in os.environ:
		statedir = os.environ["HOME"] + "/.rawdog"
	else:
		statedir = None
	verbose = False
	locking = True
	no_lock_wait = False
	for o, a in optlist:
		if o == "--help":
			usage()
			return 0
		elif o in ("-d", "--dir"):
			statedir = a
		elif o in ("-v", "--verbose"):
			verbose = True
		elif o in ("-N", "--no-locking"):
			locking = False
		elif o in ("-W", "--no-lock-wait"):
			no_lock_wait = True
	if statedir is None:
		print "$HOME not set and state dir not explicitly specified; please use -d/--dir"
		return 1

	try:
		os.chdir(statedir)
	except OSError:
		print "No " + statedir + " directory"
		return 1

	sys.path.append(".")

	config = Config(locking)
	def load_config(fn):
		try:
			config.load(fn)
		except ConfigError, err:
			print >>sys.stderr, "In " + fn + ":"
			print >>sys.stderr, err
			return 1
		if verbose:
			config["verbose"] = True
	load_config("config")

	persister, rawdog = load_persisted("state", Rawdog, config, no_lock_wait)
	if rawdog is None:
		return 0
	if not rawdog.check_state_version():
		print "The state file " + statedir + "/state was created by an older"
		print "version of rawdog, and cannot be read by this version."
		print "Removing the state file will fix it."
		return 1

	rawdog.sync_from_config(config)

	plugins.call_hook("startup", rawdog, config)

	for o, a in optlist:
		if o in ("-u", "--update"):
			rawdog.update(config)
		elif o in ("-f", "--update-feed"):
			rawdog.update(config, a)
		elif o in ("-l", "--list"):
			rawdog.list(config)
		elif o in ("-w", "--write"):
			rawdog.write(config)
		elif o in ("-c", "--config"):
			load_config(a)
			rawdog.sync_from_config(config)
		elif o in ("-t", "--show-template"):
			rawdog.show_template(config)
		elif o in ("-T", "--show-itemtemplate"):
			rawdog.show_itemtemplate(config)
		elif o in ("-a", "--add"):
			add_feed("config", a, rawdog, config)
			config.reload()
			rawdog.sync_from_config(config)
		elif o in ("-r", "--remove"):
			remove_feed("config", a, config)
			config.reload()
			rawdog.sync_from_config(config)

	plugins.call_hook("shutdown", rawdog, config)

	save_persisted(persister, config)

	return 0

