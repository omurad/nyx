# Copyright 2016, Damian Johnson and The Tor Project
# See LICENSE for licensing information

"""
Listing of the currently established connections tor has made.
"""

import re
import time
import collections
import curses
import itertools
import threading

import nyx.curses
import nyx.panel
import nyx.popups
import nyx.tracker

from nyx.curses import WHITE, NORMAL, BOLD, HIGHLIGHT
from nyx import tor_controller

from stem.control import Listener
from stem.util import datetime_to_unix, conf, connection, enum, str_tools

try:
  # added in python 3.2
  from functools import lru_cache
except ImportError:
  from stem.util.lru_cache import lru_cache

# height of the detail panel content, not counting top and bottom border

DETAILS_HEIGHT = 7

EXIT_USAGE_WIDTH = 15
UPDATE_RATE = 5  # rate in seconds at which we refresh

# cached information from our last _update() call

LAST_RETRIEVED_HS_CONF = None
LAST_RETRIEVED_CIRCUITS = None

# Connection Categories:
#   Inbound      Relay connection, coming to us.
#   Outbound     Relay connection, leaving us.
#   Exit         Outbound relay connection leaving the Tor network.
#   Hidden       Connections to a hidden service we're providing.
#   Socks        Socks connections for applications using Tor.
#   Circuit      Circuits our tor client has created.
#   Directory    Fetching tor consensus information.
#   Control      Tor controller (nyx, vidalia, etc).

Category = enum.Enum('INBOUND', 'OUTBOUND', 'EXIT', 'HIDDEN', 'SOCKS', 'CIRCUIT', 'DIRECTORY', 'CONTROL')
SortAttr = enum.Enum('CATEGORY', 'UPTIME', 'IP_ADDRESS', 'PORT', 'FINGERPRINT', 'NICKNAME', 'COUNTRY')
LineType = enum.Enum('CONNECTION', 'CIRCUIT_HEADER', 'CIRCUIT')

Line = collections.namedtuple('Line', [
  'entry',
  'line_type',
  'connection',
  'circuit',
  'fingerprint',
  'nickname',
  'locale',
])


def conf_handler(key, value):
  if key == 'features.connection.order':
    return conf.parse_enum_csv(key, value[0], SortAttr, 3)


CONFIG = conf.config_dict('nyx', {
  'attr.connection.category_color': {},
  'attr.connection.sort_color': {},
  'features.connection.resolveApps': True,
  'features.connection.order': [SortAttr.CATEGORY, SortAttr.IP_ADDRESS, SortAttr.UPTIME],
  'features.connection.showIps': True,
}, conf_handler)


class Entry(object):
  @staticmethod
  @lru_cache()
  def from_connection(connection):
    return ConnectionEntry(connection)

  @staticmethod
  @lru_cache()
  def from_circuit(circuit):
    return CircuitEntry(circuit)

  def get_lines(self):
    """
    Provides individual lines of connection information.

    :returns: **list** of **ConnectionLine** concerning this entry
    """

    raise NotImplementedError('should be implemented by subclasses')

  def get_type(self):
    """
    Provides our best guess at the type of connection this is.

    :returns: **Category** for the connection's type
    """

    raise NotImplementedError('should be implemented by subclasses')

  def is_private(self):
    """
    Checks if information about this endpoint should be scrubbed. Relaying
    etiquette (and wiretapping laws) say these are bad things to look at so
    DON'T CHANGE THIS UNLESS YOU HAVE A DAMN GOOD REASON!

    :returns: **bool** indicating if connection information is sensive or not
    """

    raise NotImplementedError('should be implemented by subclasses')

  def sort_value(self, attr):
    """
    Provides a heuristic for sorting by a given value.

    :param SortAttr attr: sort attribute to provide a heuristic for

    :returns: comparable value for sorting
    """

    line = self.get_lines()[0]
    at_end = 'z' * 20

    if attr == SortAttr.IP_ADDRESS:
      if self.is_private():
        return 255 ** 4  # orders at the end

      ip_value = 0

      for octet in line.connection.remote_address.split('.'):
        ip_value = ip_value * 255 + int(octet)

      return ip_value * 65536 + line.connection.remote_port
    elif attr == SortAttr.PORT:
      return line.connection.remote_port
    elif attr == SortAttr.FINGERPRINT:
      return line.fingerprint if line.fingerprint else at_end
    elif attr == SortAttr.NICKNAME:
      return line.nickname if line.nickname else at_end
    elif attr == SortAttr.CATEGORY:
      return Category.index_of(self.get_type())
    elif attr == SortAttr.UPTIME:
      return line.connection.start_time
    elif attr == SortAttr.COUNTRY:
      return line.locale if (line.locale and not self.is_private()) else at_end
    else:
      return ''


class ConnectionEntry(Entry):
  def __init__(self, connection):
    self._connection = connection

  @lru_cache()
  def get_lines(self):
    fingerprint, nickname, locale = None, None, None

    if self.get_type() in (Category.OUTBOUND, Category.CIRCUIT, Category.DIRECTORY, Category.EXIT):
      fingerprint = nyx.tracker.get_consensus_tracker().get_relay_fingerprints(self._connection.remote_address).get(self._connection.remote_port)

      if fingerprint:
        nickname = nyx.tracker.get_consensus_tracker().get_relay_nickname(fingerprint)
        locale = tor_controller().get_info('ip-to-country/%s' % self._connection.remote_address, None)

    return [Line(self, LineType.CONNECTION, self._connection, None, fingerprint, nickname, locale)]

  @lru_cache()
  def get_type(self):
    controller = tor_controller()

    if self._connection.local_port in controller.get_ports(Listener.OR, []):
      return Category.INBOUND
    elif self._connection.local_port in controller.get_ports(Listener.DIR, []):
      return Category.INBOUND
    elif self._connection.local_port in controller.get_ports(Listener.SOCKS, []):
      return Category.SOCKS
    elif self._connection.local_port in controller.get_ports(Listener.CONTROL, []):
      return Category.CONTROL

    if LAST_RETRIEVED_HS_CONF:
      for hs_config in LAST_RETRIEVED_HS_CONF.values():
        if self._connection.remote_port == hs_config['HiddenServicePort']:
          return Category.HIDDEN

    fingerprint = nyx.tracker.get_consensus_tracker().get_relay_fingerprints(self._connection.remote_address).get(self._connection.remote_port)

    if fingerprint and LAST_RETRIEVED_CIRCUITS:
      for circ in LAST_RETRIEVED_CIRCUITS:
        if circ.path and len(circ.path) == 1 and circ.path[0][0] == fingerprint and circ.status == 'BUILT':
          return Category.DIRECTORY  # one-hop circuit to retrieve directory information
    else:
      # not a known relay, might be an exit connection

      exit_policy = controller.get_exit_policy(None)

      if exit_policy and exit_policy.can_exit_to(self._connection.remote_address, self._connection.remote_port):
        return Category.EXIT

    return Category.OUTBOUND

  @lru_cache()
  def is_private(self):
    if not CONFIG['features.connection.showIps']:
      return True

    if self.get_type() == Category.INBOUND:
      controller = tor_controller()

      if controller.is_user_traffic_allowed().inbound:
        return len(nyx.tracker.get_consensus_tracker().get_relay_fingerprints(self._connection.remote_address)) == 0
    elif self.get_type() == Category.EXIT:
      # DNS connections exiting us aren't private (since they're hitting our
      # resolvers). Everything else is.

      return not (self._connection.remote_port == 53 and self._connection.protocol == 'udp')

    return False  # for everything else this isn't a concern


class CircuitEntry(Entry):
  def __init__(self, circuit):
    self._circuit = circuit

  @lru_cache()
  def get_lines(self):
    def line(fingerprint, line_type):
      address, port, nickname, locale = '0.0.0.0', 0, None, None
      consensus_tracker = nyx.tracker.get_consensus_tracker()

      if fingerprint is not None:
        address, port = consensus_tracker.get_relay_address(fingerprint, ('192.168.0.1', 0))
        nickname = consensus_tracker.get_relay_nickname(fingerprint)
        locale = tor_controller().get_info('ip-to-country/%s' % address, None)

      connection = nyx.tracker.Connection(datetime_to_unix(self._circuit.created), False, '127.0.0.1', 0, address, port, 'tcp', False)
      return Line(self, line_type, connection, self._circuit, fingerprint, nickname, locale)

    header_line = line(self._circuit.path[-1][0] if self._circuit.status == 'BUILT' else None, LineType.CIRCUIT_HEADER)
    return [header_line] + [line(fp, LineType.CIRCUIT) for fp, _ in self._circuit.path]

  def get_type(self):
    return Category.CIRCUIT

  def is_private(self):
    return False


class ConnectionPanel(nyx.panel.Panel, threading.Thread):
  """
  Listing of connections tor is making, with information correlated against
  the current consensus and other data sources.
  """

  def __init__(self):
    nyx.panel.Panel.__init__(self, 'connections')
    threading.Thread.__init__(self)
    self.setDaemon(True)

    self._scroller = nyx.curses.CursorScroller()
    self._entries = []            # last fetched display entries
    self._show_details = False    # presents the details panel if true
    self._sort_order = CONFIG['features.connection.order']

    self._last_resource_fetch = -1  # timestamp of the last ConnectionResolver results used

    self._pause_condition = threading.Condition()
    self._halt = False  # terminates thread if true

    # Tracks exiting port and client country statistics

    self._client_locale_usage = {}
    self._exit_port_usage = {}
    self._counted_connections = set()

    # If we're a bridge and been running over a day then prepopulates with the
    # last day's clients.

    bridge_clients = tor_controller().get_info('status/clients-seen', None)

    if bridge_clients:
      # Response has a couple arguments...
      # TimeStarted="2011-08-17 15:50:49" CountrySummary=us=16,de=8,uk=8

      country_summary = None

      for line in bridge_clients.split():
        if line.startswith('CountrySummary='):
          country_summary = line[15:]
          break

      if country_summary:
        for entry in country_summary.split(','):
          if re.match('^..=[0-9]+$', entry):
            locale, count = entry.split('=', 1)
            self._client_locale_usage[locale] = int(count)

  def show_sort_dialog(self):
    """
    Provides a dialog for sorting our connections.
    """

    sort_colors = dict([(attr, CONFIG['attr.connection.sort_color'].get(attr, WHITE)) for attr in SortAttr])
    results = nyx.popups.show_sort_dialog('Connection Ordering:', SortAttr, self._sort_order, sort_colors)

    if results:
      self._sort_order = results
      self._entries = sorted(self._entries, key = lambda entry: [entry.sort_value(attr) for attr in self._sort_order])

  def run(self):
    """
    Keeps connections listing updated, checking for new entries at a set rate.
    """

    last_ran = -1

    while not self._halt:
      if self.is_paused() or not tor_controller().is_alive() or (time.time() - last_ran) < UPDATE_RATE:
        with self._pause_condition:
          if not self._halt:
            self._pause_condition.wait(0.2)

        continue  # done waiting, try again

      self._update()
      self.redraw(True)

      # TODO: The following is needed to show results *but* causes curses to
      # flicker. For our plans on this see...
      #
      #   https://trac.torproject.org/projects/tor/ticket/18547#comment:1

      # if last_ran == -1:
      #   nyx.tracker.get_consensus_tracker().update(tor_controller().get_network_statuses([]))

      last_ran = time.time()

  def key_handlers(self):
    def _scroll(key):
      page_height = self.get_preferred_size()[0] - 1

      if self._show_details:
        page_height -= (DETAILS_HEIGHT + 1)

      lines = list(itertools.chain.from_iterable([entry.get_lines() for entry in self._entries]))
      is_changed = self._scroller.handle_key(key, lines, page_height)

      if is_changed:
        self.redraw(True)

    def _show_details():
      self._show_details = not self._show_details
      self.redraw(True)

    def _show_descriptor():
      entries = self._entries

      while True:
        lines = list(itertools.chain.from_iterable([entry.get_lines() for entry in entries]))
        selected = self._scroller.selection(lines)

        if not selected:
          break

        def is_close_key(key):
          return key.is_selection() or key.match('d') or key.match('left') or key.match('right')

        color = CONFIG['attr.connection.category_color'].get(selected.entry.get_type(), WHITE)
        key = nyx.popups.show_descriptor(selected.fingerprint, color, is_close_key)

        if not key or key.is_selection() or key.match('d'):
          break  # closes popup
        elif key.match('left'):
          _scroll(nyx.curses.KeyInput(curses.KEY_UP))
        elif key.match('right'):
          _scroll(nyx.curses.KeyInput(curses.KEY_DOWN))

      self.redraw(True)

    def _pick_connection_resolver():
      connection_tracker = nyx.tracker.get_connection_tracker()
      resolver = connection_tracker.get_custom_resolver()
      options = ['auto'] + list(connection.Resolver) + list(nyx.tracker.CustomResolver)

      selected = nyx.popups.show_list_selector('Connection Resolver:', options, resolver if resolver else 'auto')
      connection_tracker.set_custom_resolver(None if selected == 'auto' else selected)

      self.redraw(True)

    def _show_client_locales():
      nyx.popups.show_counts('Client Locales', self._client_locale_usage)

    def _show_exiting_port_usage():
      counts = {}
      key_width = max(map(len, self._exit_port_usage.keys()))

      for k, v in self._exit_port_usage.items():
        usage = connection.port_usage(k)

        if usage:
          k = k.ljust(key_width + 3) + usage.ljust(EXIT_USAGE_WIDTH)

        counts[k] = v

      nyx.popups.show_counts('Exiting Port Usage', counts)

    resolver = nyx.tracker.get_connection_tracker().get_custom_resolver()
    user_traffic_allowed = tor_controller().is_user_traffic_allowed()

    options = [
      nyx.panel.KeyHandler('arrows', 'scroll up and down', _scroll, key_func = lambda key: key.is_scroll()),
      nyx.panel.KeyHandler('enter', 'show connection details', _show_details, key_func = lambda key: key.is_selection()),
      nyx.panel.KeyHandler('d', 'raw consensus descriptor', _show_descriptor),
      nyx.panel.KeyHandler('s', 'sort ordering', self.show_sort_dialog),
      nyx.panel.KeyHandler('r', 'connection resolver', _pick_connection_resolver, 'auto' if resolver is None else resolver),
    ]

    if user_traffic_allowed.inbound:
      options.append(nyx.panel.KeyHandler('c', 'client locale usage summary', _show_client_locales))

    if user_traffic_allowed.outbound:
      options.append(nyx.panel.KeyHandler('e', 'exit port usage summary', _show_exiting_port_usage))

    return tuple(options)

  def draw(self, width, height):
    controller = tor_controller()
    entries = self._entries

    lines = list(itertools.chain.from_iterable([entry.get_lines() for entry in entries]))
    is_showing_details = self._show_details and lines
    details_offset = DETAILS_HEIGHT + 1 if is_showing_details else 0
    selected, scroll = self._scroller.selection(lines, height - details_offset - 1)

    if self.is_paused():
      current_time = self.get_pause_time()
    elif not controller.is_alive():
      current_time = controller.connection_time()
    else:
      current_time = time.time()

    is_scrollbar_visible = len(lines) > height - details_offset - 1
    scroll_offset = 2 if is_scrollbar_visible else 0

    self._draw_title(entries, self._show_details)

    if is_showing_details:
      self._draw_details(selected, width, is_scrollbar_visible)

    if is_scrollbar_visible:
      self.add_scroll_bar(scroll, scroll + height - details_offset - 1, len(lines), 1 + details_offset)

    for line_number in range(scroll, len(lines)):
      y = line_number + details_offset + 1 - scroll
      self._draw_line(scroll_offset, y, lines[line_number], lines[line_number] == selected, width - scroll_offset, current_time)

      if y >= height:
        break

  def _draw_title(self, entries, showing_details):
    """
    Panel title with the number of connections we presently have.
    """

    if showing_details:
      self.addstr(0, 0, 'Connection Details:', HIGHLIGHT)
    elif not entries:
      self.addstr(0, 0, 'Connections:', HIGHLIGHT)
    else:
      counts = collections.Counter([entry.get_type() for entry in entries])
      count_labels = ['%i %s' % (counts[category], category.lower()) for category in Category if counts[category]]
      self.addstr(0, 0, 'Connections (%s):' % ', '.join(count_labels), HIGHLIGHT)

  def _draw_details(self, selected, width, is_scrollbar_visible):
    """
    Shows detailed information about the selected connection.
    """

    attr = (CONFIG['attr.connection.category_color'].get(selected.entry.get_type(), WHITE), BOLD)

    if selected.line_type == LineType.CIRCUIT_HEADER and selected.circuit.status != 'BUILT':
      self.addstr(1, 2, 'Building Circuit...', *attr)
    else:
      address = '<scrubbed>' if selected.entry.is_private() else selected.connection.remote_address
      self.addstr(1, 2, 'address: %s:%s' % (address, selected.connection.remote_port), *attr)
      self.addstr(2, 2, 'locale: %s' % ('??' if selected.entry.is_private() else (selected.locale if selected.locale else '??')), *attr)

      matches = nyx.tracker.get_consensus_tracker().get_relay_fingerprints(selected.connection.remote_address)

      if not matches:
        self.addstr(3, 2, 'No consensus data found', *attr)
      elif len(matches) == 1 or selected.connection.remote_port in matches:
        controller = tor_controller()
        fingerprint = matches.values()[0] if len(matches) == 1 else matches[selected.connection.remote_port]
        router_status_entry = controller.get_network_status(fingerprint, None)

        self.addstr(2, 15, 'fingerprint: %s' % fingerprint, *attr)

        if router_status_entry:
          dir_port_label = 'dirport: %s' % router_status_entry.dir_port if router_status_entry.dir_port else ''
          self.addstr(3, 2, 'nickname: %-25s orport: %-10s %s' % (router_status_entry.nickname, router_status_entry.or_port, dir_port_label), *attr)
          self.addstr(4, 2, 'published: %s' % router_status_entry.published.strftime("%H:%M %m/%d/%Y"), *attr)
          self.addstr(5, 2, 'flags: %s' % ', '.join(router_status_entry.flags), *attr)

          server_descriptor = controller.get_server_descriptor(fingerprint, None)

          if server_descriptor:
            policy_label = server_descriptor.exit_policy.summary() if server_descriptor.exit_policy else 'unknown'
            self.addstr(6, 2, 'exit policy: %s' % policy_label, *attr)
            self.addstr(4, 38, 'os: %-14s version: %s' % (server_descriptor.operating_system, server_descriptor.tor_version), *attr)

            if server_descriptor.contact:
              self.addstr(7, 2, 'contact: %s' % server_descriptor.contact, *attr)
      else:
        self.addstr(3, 2, 'Multiple matches, possible fingerprints are:', *attr)

        for i, port in enumerate(sorted(matches.keys())):
          is_last_line, remaining_relays = i == 3, len(matches) - i

          if not is_last_line or remaining_relays == 1:
            self.addstr(4 + i, 2, '%i. or port: %-5s fingerprint: %s' % (i + 1, port, matches[port]), *attr)
          else:
            self.addstr(4 + i, 2, '... %i more' % remaining_relays, *attr)

          if is_last_line:
            break

    # draw the border, with a 'T' pipe if connecting with the scrollbar

    self.draw_box(0, 0, width, DETAILS_HEIGHT + 2)

    if is_scrollbar_visible:
      self.addch(DETAILS_HEIGHT + 1, 1, curses.ACS_TTEE)

  def _draw_line(self, x, y, line, is_selected, width, current_time):
    attr = [CONFIG['attr.connection.category_color'].get(line.entry.get_type(), WHITE)]
    attr.append(HIGHLIGHT if is_selected else NORMAL)

    self.addstr(y, x, ' ' * (width - x), *attr)

    if line.line_type == LineType.CIRCUIT:
      if line.circuit.path[-1][0] == line.fingerprint:
        prefix = (ord(' '), curses.ACS_LLCORNER, curses.ACS_HLINE, ord(' '))
      else:
        prefix = (ord(' '), curses.ACS_VLINE, ord(' '), ord(' '))

      for char in prefix:
        x = self.addch(y, x, char)
    else:
      x += 1  # offset from edge

    self._draw_address_column(x, y, line, attr)
    self._draw_line_details(57, y, line, width - 57 - 20, attr)
    self._draw_right_column(width - 18, y, line, current_time, attr)

  def _draw_address_column(self, x, y, line, attr):
    src = tor_controller().get_info('address', line.connection.local_address)
    src += ':%s' % line.connection.local_port if line.line_type == LineType.CONNECTION else ''

    if line.line_type == LineType.CIRCUIT_HEADER and line.circuit.status != 'BUILT':
      dst = 'Building...'
    else:
      dst = '<scrubbed>' if line.entry.is_private() else line.connection.remote_address
      dst += ':%s' % line.connection.remote_port

      if line.entry.get_type() == Category.EXIT:
        purpose = connection.port_usage(line.connection.remote_port)

        if purpose:
          dst += ' (%s)' % str_tools.crop(purpose, 26 - len(dst) - 3)
      elif not tor_controller().is_geoip_unavailable() and not line.entry.is_private():
        dst += ' (%s)' % (line.locale if line.locale else '??')

    if line.entry.get_type() in (Category.INBOUND, Category.SOCKS, Category.CONTROL):
      dst, src = src, dst

    if line.line_type == LineType.CIRCUIT:
      self.addstr(y, x, dst, *attr)
    else:
      self.addstr(y, x, '%-21s  -->  %-26s' % (src, dst), *attr)

  def _draw_line_details(self, x, y, line, width, attr):
    if line.line_type == LineType.CIRCUIT_HEADER:
      comp = ['Purpose: %s' % line.circuit.purpose.capitalize(), ', Circuit ID: %s' % line.circuit.id]
    elif line.entry.get_type() in (Category.SOCKS, Category.HIDDEN, Category.CONTROL):
      try:
        port = line.connection.local_port if line.entry.get_type() == Category.HIDDEN else line.connection.remote_port
        process = nyx.tracker.get_port_usage_tracker().fetch(port)
        comp = ['%s (%s)' % (process.name, process.pid) if process.pid else process.name]
      except nyx.tracker.UnresolvedResult:
        comp = ['resolving...']
      except nyx.tracker.UnknownApplication:
        comp = ['UNKNOWN']
    else:
      comp = ['%-40s' % (line.fingerprint if line.fingerprint else 'UNKNOWN'), '  ' + (line.nickname if line.nickname else 'UNKNOWN')]

    for entry in comp:
      if width >= x + len(entry):
        x = self.addstr(y, x, entry, *attr)
      else:
        return

  def _draw_right_column(self, x, y, line, current_time, attr):
    if line.line_type == LineType.CIRCUIT:
      circ_path = [fp for fp, _ in line.circuit.path]
      circ_index = circ_path.index(line.fingerprint)

      if circ_index == len(circ_path) - 1:
        placement_type = 'Exit' if line.circuit.status == 'BUILT' else 'Extending'
      elif circ_index == 0:
        placement_type = 'Guard'
      else:
        placement_type = 'Middle'

      self.addstr(y, x + 4, '%i / %s' % (circ_index + 1, placement_type), *attr)
    else:
      x = self.addstr(y, x, '+' if line.connection.is_legacy else ' ', *attr)
      x = self.addstr(y, x, '%5s' % str_tools.time_label(current_time - line.connection.start_time, 1), *attr)
      x = self.addstr(y, x, ' (', *attr)
      x = self.addstr(y, x, line.entry.get_type().upper(), BOLD, *attr)
      x = self.addstr(y, x, ')', *attr)

  def stop(self):
    """
    Halts further resolutions and terminates the thread.
    """

    with self._pause_condition:
      self._halt = True
      self._pause_condition.notifyAll()

  def _update(self):
    """
    Fetches the newest resolved connections.
    """

    global LAST_RETRIEVED_CIRCUITS, LAST_RETRIEVED_HS_CONF

    controller = tor_controller()
    LAST_RETRIEVED_CIRCUITS = controller.get_circuits([])
    LAST_RETRIEVED_HS_CONF = controller.get_hidden_service_conf({})

    conn_resolver = nyx.tracker.get_connection_tracker()
    current_resolution_count = conn_resolver.run_counter()

    if not conn_resolver.is_alive():
      return  # if we're not fetching connections then this is a no-op
    elif current_resolution_count == self._last_resource_fetch:
      return  # no new connections to process

    new_entries = [Entry.from_connection(conn) for conn in conn_resolver.get_value()]

    for circ in LAST_RETRIEVED_CIRCUITS:
      # Skips established single-hop circuits (these are for directory
      # fetches, not client circuits)

      if not (circ.status == 'BUILT' and len(circ.path) == 1):
        new_entries.append(Entry.from_circuit(circ))

    # update stats for client and exit connections

    for entry in new_entries:
      line = entry.get_lines()[0]

      if entry.is_private() and line.connection not in self._counted_connections:
        if entry.get_type() == Category.INBOUND and line.locale:
          self._client_locale_usage[line.locale] = self._client_locale_usage.get(line.locale, 0) + 1
        elif entry.get_type() == Category.EXIT:
          self._exit_port_usage[line.connection.remote_port] = self._exit_port_usage.get(line.connection.remote_port, 0) + 1

        self._counted_connections.add(line.connection)

    self._entries = sorted(new_entries, key = lambda entry: [entry.sort_value(attr) for attr in self._sort_order])
    self._last_resource_fetch = current_resolution_count

    if CONFIG['features.connection.resolveApps']:
      local_ports, remote_ports = [], []

      for entry in new_entries:
        line = entry.get_lines()[0]

        if entry.get_type() in (Category.SOCKS, Category.CONTROL):
          local_ports.append(line.connection.remote_port)
        elif entry.get_type() == Category.HIDDEN:
          remote_ports.append(line.connection.local_port)

      nyx.tracker.get_port_usage_tracker().query(local_ports, remote_ports)
