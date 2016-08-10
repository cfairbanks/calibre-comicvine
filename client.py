"""
calibre_plugins.comicvine - A calibre metadata source for comicvine
"""
import logging
import random
import time
import threading
import pyfscache
import os
import pycomicvine

from urllib2 import HTTPError
from calibre.utils.config import JSONConfig
from config import PREFS
from pycomicvine.error import RateLimitExceededError


class TokenBucket(object):
  def __init__(self):
    self.lock = threading.RLock()
    params = JSONConfig('plugins/comicvine_tokens')
    params.defaults['tokens'] = 0
    params.defaults['update'] = time.time()
    self.params = params

  def consume(self):
    with self.lock:
      self.params.refresh()
      time_since_last_request = time.time() - self.params['update']
      interval = PREFS['request_interval']
      while self.tokens < 1:
        if interval > time_since_last_request:
          delay = interval - time_since_last_request
        else:
          delay = interval
        logging.warn('%0.2f seconds to next request token', delay)
        time.sleep(delay)
      self.params['tokens'] -= 1

  @property
  def tokens(self):
    with self.lock:
      self.params.refresh()

      if self.params['tokens'] < PREFS['request_batch_size']:
        now = time.time()
        elapsed = now - self.params['update']
        if elapsed > 0:
          new_tokens = int(elapsed * (1.0 / PREFS['request_interval']))
          if new_tokens:
            if new_tokens + self.params['tokens'] < PREFS['request_batch_size']:
              self.params['tokens'] += new_tokens
            else:
              self.params['tokens'] = PREFS['request_batch_size']
            self.params['update'] = now
    return self.params['tokens']


token_bucket = TokenBucket()


def retry_on_comicvine_error():
  """
  Decorator for functions that access the comicvine api.

  Retries the decorated function on error.
  """
  pycomicvine.api_key = PREFS['api_key']
  retries = PREFS['retries']

  def wrap_function(target_function):
    """Closure for the retry function, giving access to decorator arguments."""

    def retry_function(*args, **kwargs):
      """
      Decorate function to retry on error.

      The comicvine API can be a little flaky, so retry on error to make
      sure the error is real.

      If retries is exceeded will raise the original exception.
      """
      for retry in range(1, retries + 1):
        token_bucket.consume()

        def handle_rate_limit(e):
          logging.warn('API Rate limit exceeded %s', e.message)
          raise

        def handle_exception(e):
          logging.warn('Calling %r failed on attempt %d/%d - args [%r %r], exception %s',
                       target_function, retry, retries, args, kwargs, e.message)

          if retry >= retries:
            raise
          else:
            time.sleep(random.random() / 2 + 0.1)

        try:
          return target_function(*args, **kwargs)
        except RateLimitExceededError as e:
          handle_rate_limit(e)
        except HTTPError as e:
          if e.code == 420:
            handle_rate_limit(e)
          else:
            handle_exception(e)
        except Exception as e:
          handle_exception(e)

    return retry_function

  return wrap_function


def cache_comicvine(cache_name):
  """
  Decorator for instance methods on the comicvine wrapper.
  """

  def wrap_function(target_function):
    temp_directory = os.getenv('TMPDIR')

    if temp_directory is not None:
      path = '%s/calibre-comicvine/%s' % (temp_directory, cache_name)
      cache_it = pyfscache.FSCache(path, hours=1)

      def instance_function(*args, **kwargs):
        self = args[0]

        @cache_it
        def cached_function(*args, **kwargs):
          return target_function(self, *args, **kwargs)

        return cached_function(*args[1:], **kwargs)

      return instance_function
    else:
      return target_function

  return wrap_function


class PyComicvineWrapper(object):
  def __init__(self, log):
    self.log = log

  @cache_comicvine('lookup_volume_id')
  @retry_on_comicvine_error()
  def lookup_volume_id(self, volume_id):
    self.debug('Looking up volume: %d' % volume_id)
    volume = pycomicvine.Volume(id=volume_id, field_list=['id'])

    if volume:
      self.debug("Found volume: %d" % volume_id)
      return volume.id
    else:
      self.debug("Failed to find volume: %d" % volume_id)
      return None

  @retry_on_comicvine_error()
  def lookup_issue(self, issue_id):
    self.debug('Looking up issue: %d' % issue_id)
    issue = pycomicvine.Issue(issue_id,
                              field_list=['id',
                                          'name',
                                          'volume',
                                          'issue_number',
                                          'person_credits',
                                          'description',
                                          'store_date',
                                          'cover_date'])
    if issue and issue.volume:
      self.debug("Found issue: %d %s #%s" % (issue_id, issue.volume.name, issue.issue_number))
      return issue
    elif issue:
      self.warn("Found issue but failed to find issue volume: %d" % issue_id)
      return None
    else:
      self.warn("Failed to find issue: %d" % issue_id)
      return None

  @cache_comicvine('lookup_issue_image_urls')
  @retry_on_comicvine_error()
  def lookup_issue_image_urls(self, issue_id, get_best_cover=False):
    """Retrieve cover urls, in quality order."""
    self.debug('Looking up issue image: %d' % issue_id)
    issue = pycomicvine.Issue(issue_id, field_list=['image'])

    if issue and issue.image:
      urls = []
      for url_key in ['super_url', 'medium_url', 'small_url']:
        if url_key in issue.image:
          urls.append(issue.image[url_key])
          if get_best_cover:
            break

      self.debug("Found issue image urls: %d %s" % (issue_id, urls))
      return urls
    elif issue:
      self.warn("Found issue but failed to find issue image: %d" % issue_id)
      return []
    else:
      self.warn("Failed to find issue: %d" % issue_id)
      return []

  @retry_on_comicvine_error()
  def search_for_authors(self, author_tokens):
    """Find people that match the author tokens."""
    if author_tokens and author_tokens != ['Unknown']:
      filters = ['name:%s' % author_token for author_token in author_tokens]
      filter_string = ','.join(filters)
      self.debug("Searching for author: %s" % filter_string)
      authors = pycomicvine.People(filter=filter_string, field_list=['id'])
      self.debug("%d matches found" % len(authors))
      return authors
    else:
      return []

  @cache_comicvine('search_for_issue_ids')
  @retry_on_comicvine_error()
  def search_for_issue_ids(self, filters):
    filter_string = ','.join(filters)
    self.debug('Searching for issues: %s' % filter_string)
    ids = [issue.id for issue in pycomicvine.Issues(filter=filter_string, field_list=['id'])]
    self.debug('%d issue ID matches found: %s' % (len(ids), ids))
    return ids

  @cache_comicvine('search_for_volume_ids')
  @retry_on_comicvine_error()
  def search_for_volume_ids(self, title_tokens):
    query_string = ' AND '.join(title_tokens)
    self.debug('Searching for volumes: %s' % query_string)
    candidate_volume_ids = [volume.id for volume in pycomicvine.Volumes.search(query=query_string, field_list=['id'])]
    self.debug('%d volume ID matches found: %s' % (len(candidate_volume_ids), candidate_volume_ids))
    return candidate_volume_ids

  def debug(self, message):
    self.log.debug(message)
    # uncomment for calibre-debug testing
    # print(message)

  def warn(self, message):
    self.log.warn(message)
    # uncomment for calibre-debug testing
    # print(message)