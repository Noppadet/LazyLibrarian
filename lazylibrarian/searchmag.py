import time
import threading
import urllib2
import re
import datetime

import lazylibrarian

from lazylibrarian import logger, database, formatter, providers, notifiers, common, postprocess

from lib.fuzzywuzzy import fuzz
from lazylibrarian.searchtorrents import TORDownloadMethod
from lazylibrarian.searchnzb import NZBDownloadMethod


def search_magazines(mags=None, reset=False):
    # produce a list of magazines to search for, tor, nzb, torznab

    myDB = database.DBConnection()
    searchlist = []
    threading.currentThread().name = "SEARCHMAGS"

    if mags is None:  # backlog search
        searchmags = myDB.select('SELECT Title, LastAcquired, \
                                 IssueDate from magazines WHERE Status="Active"')
    else:
        searchmags = []
        for magazine in mags:
            searchmags_temp = myDB.select('SELECT Title, LastAcquired, IssueDate from magazines \
                                          WHERE Title="%s" AND Status="Active"' % (magazine['bookid']))
            for terms in searchmags_temp:
                searchmags.append(terms)

    if len(searchmags) == 1:
        logger.info('Searching for one magazine')
    else:
        logger.info('Searching for %i magazines' % len(searchmags))

    for searchmag in searchmags:
        bookid = searchmag[0]
        searchterm = searchmag[0]
        # frequency = searchmag[1]
        # last_acquired = searchmag[2]
        # issue_date = searchmag[3]

        dic = {'...': '', ' & ': ' ', ' = ': ' ', '?': '', '$': 's', ' + ': ' ', '"': '', ',': '', '*': ''}

        searchterm = formatter.latinToAscii(formatter.replace_all(searchterm, dic))
        searchterm = re.sub('[\.\-\/]', ' ', searchterm).encode('utf-8')
        searchlist.append({"bookid": bookid, "searchterm": searchterm})

    if searchlist == []:
        logger.warn('There is nothing to search for.  Mark some magazines as active.')

    for book in searchlist:

        resultlist = []
        tor_resultlist = []
        if lazylibrarian.USE_NZB():
            resultlist, nproviders = providers.IterateOverNewzNabSites(book, 'mag')
            if not nproviders:
                logger.warn('No nzb providers are set. Check config for NEWZNAB or TORZNAB providers')

        if lazylibrarian.USE_TOR():
            tor_resultlist, nproviders = providers.IterateOverTorrentSites(book, 'mag')
            if not nproviders:
                logger.warn('No torrent providers are set. Check config for TORRENT providers')

            for item in tor_resultlist:  # reformat the torrent results so they look like nzbs
                resultlist.append({
                    'bookid': item['bookid'],
                    'nzbprov': item['tor_prov'],
                    'nzbtitle': item['tor_title'],
                    'nzburl': item['tor_url'],
                    'nzbdate': 'Fri, 01 Jan 1970 00:00:00 +0100',  # fake date as none returned from torrents
                    'nzbsize': item['tor_size'],
                    'nzbmode': 'torrent'
                })

        if not resultlist:
            logger.debug("Adding magazine %s to queue." % book['searchterm'])

        else:
            bad_regex = 0
            bad_date = 0
            old_date = 0
            total_nzbs = 0
            new_date = 0
            to_snatch = 0
            maglist = []
            issues = []
            reject_list = formatter.getList(lazylibrarian.REJECT_WORDS)
            for nzb in resultlist:
                total_nzbs = total_nzbs + 1
                bookid = nzb['bookid']
                nzbtitle = (u'%s' % nzb['nzbtitle'])
                nzbtitle = nzbtitle.replace('"', '').replace("'", "")  # suppress " in titles
                nzburl = nzb['nzburl']
                nzbprov = nzb['nzbprov']
                nzbdate_temp = nzb['nzbdate']
                nzbsize_temp = nzb['nzbsize']
                if nzbsize_temp is None:  # not all torrents returned by torznab have a size
                    nzbsize_temp = 1000
                nzbsize = str(round(float(nzbsize_temp) / 1048576, 2)) + ' MB'
                nzbdate = formatter.nzbdate2format(nzbdate_temp)
                nzbmode = nzb['nzbmode']

                checkifmag = myDB.select('SELECT * from magazines WHERE Title="%s"' % bookid)
                if checkifmag:
                    for results in checkifmag:
                        control_date = results['IssueDate']
                        # frequency = results['Frequency']
                        # regex = results['Regex']

                    nzbtitle_formatted = nzbtitle.replace('.', ' ').replace('-', ' ').replace('/', ' ').replace(
                        '+', ' ').replace('_', ' ').replace('(', '').replace(')', '').strip()
                    # Need to make sure that substrings of magazine titles don't get found
                    # (e.g. Maxim USA will find Maximum PC USA) - token_set_ratio takes care of this
                    # keyword_check = nzbtitle_formatted.replace(bookid, '')
                    # remove extra spaces if they're in a row
                    nzbtitle_exploded_temp = " ".join(nzbtitle_formatted.split())
                    nzbtitle_exploded = nzbtitle_exploded_temp.split(' ')

                    if ' ' in bookid:
                        bookid_exploded = bookid.split(' ')
                    else:
                        bookid_exploded = [bookid]

                    # check nzb starts with magazine title, and ends with a date
                    # eg The MagPI Issue 22 - July 2015
                    # do something like check left n words match title
                    # then check last n words are a date

                    name_match = 1  # assume name matches for now
                    if len(nzbtitle_exploded) > len(bookid_exploded):  # needs to be longer as it has to include a date
                        # check (nearly) all the words in the mag title are in the nzbtitle - allow some fuzz
                        mag_title_match = fuzz.token_set_ratio(
                            common.remove_accents(bookid),
                            common.remove_accents(nzbtitle_formatted))
                        if mag_title_match < lazylibrarian.MATCH_RATIO:
                            logger.debug(
                                u"Magazine token set Match failed: " + str(
                                    mag_title_match) + "% for " + nzbtitle_formatted)
                            name_match = 0

                    lower_title = common.remove_accents(nzbtitle_formatted).lower()
                    lower_bookid = common.remove_accents(bookid).lower()
                    for word in reject_list:
                        if word in lower_title and not word in lower_bookid:
                            name_match = 0
                            logger.debug("Rejecting %s, contains %s" % (nzbtitle_formatted, word))
                            break

                    if name_match:
                        # some magazine torrent uploaders add their sig in [] or {}
                        # Fortunately for us, they always seem to add it at the end
                        # also some magazine torrent titles are "magazine_name some_form_of_date pdf"
                        # so strip all the trailing junk...
                        while nzbtitle_exploded[len(nzbtitle_exploded) - 1][0] in '[{' or \
                                nzbtitle_exploded[len(nzbtitle_exploded) - 1].lower() == 'pdf':
                                nzbtitle_exploded.pop()  # gotta love the function names

                        # need at least one word magazine title and two date components
                        if len(nzbtitle_exploded) > 2:
                            # regexA = DD MonthName YYYY OR MonthName YYYY or Issue nn MonthName YYYY
                            regexA_year = nzbtitle_exploded[len(nzbtitle_exploded) - 1]
                            regexA_month_temp = nzbtitle_exploded[len(nzbtitle_exploded) - 2]
                            regexA_month = formatter.month2num(common.remove_accents(regexA_month_temp))
                            if not regexA_year.isdigit() or int(regexA_year) < 1900 or int(regexA_year) > 2100:
                                regexA_year = 'fail'  # force date failure

                            # if frequency == "Weekly" or frequency == "BiWeekly":
                            regexA_day = nzbtitle_exploded[len(nzbtitle_exploded) - 3].zfill(2)
                            if regexA_day.isdigit():
                                if int(regexA_day) > 31:  # probably issue number nn
                                    regexA_day = '01'
                            else:
                                regexA_day = '01'  # just MonthName YYYY
                            # else:
                            # regexA_day = '01'  # monthly, or less frequent

                            try:
                                newdatish = regexA_year + '-' + regexA_month + '-' + regexA_day
                                # try to make sure the year/month/day are valid, exception if not
                                # ie don't accept day > 31, or 30 in some months
                                # also handles multiple date format named issues eg Jan 2014, 01 2014
                                # datetime will give a ValueError if not a good date or a param is not int
                                date1 = datetime.date(int(regexA_year), int(regexA_month), int(regexA_day))
                            except ValueError:
                                # regexB = MonthName DD YYYY
                                regexB_year = nzbtitle_exploded[len(nzbtitle_exploded) - 1]
                                regexB_month_temp = nzbtitle_exploded[len(nzbtitle_exploded) - 3]
                                regexB_month = formatter.month2num(common.remove_accents(regexB_month_temp))
                                regexB_day = nzbtitle_exploded[len(nzbtitle_exploded) - 2].zfill(2)
                                if not regexB_year.isdigit() or int(regexB_year) < 1900 or int(regexB_year) > 2100:
                                    regexB_year = 'fail'

                                try:
                                    newdatish = regexB_year + '-' + regexB_month + '-' + regexB_day
                                    # datetime will give a ValueError if not a good date or a param is not int
                                    date1 = datetime.date(int(regexB_year), int(regexB_month), int(regexB_day))
                                except ValueError:
                                    # regexC = YYYY MM or YYYY MM DD
                                    # (can't get MM/DD if named YYYY Issue nn)
                                    # First try  YYYY MM
                                    regexC_year = nzbtitle_exploded[len(nzbtitle_exploded) - 2]
                                    if regexC_year.isdigit() and int(regexC_year) > 1900 and int(regexC_year) < 2100:
                                        regexC_month = nzbtitle_exploded[len(nzbtitle_exploded) - 1].zfill(2)
                                        regexC_day = '01'
                                    else:  # try YYYY MM DD
                                        regexC_year = nzbtitle_exploded[len(nzbtitle_exploded) - 3]
                                        if regexC_year.isdigit() and int(regexC_year) > 1900 and int(regexC_year) < 2100:
                                            regexC_month = nzbtitle_exploded[len(nzbtitle_exploded) - 2].zfill(2)
                                            regexC_day = nzbtitle_exploded[len(nzbtitle_exploded) - 1].zfill(2)
                                        else:
                                            regexC_year = 'fail'
                                    try:
                                        newdatish = regexC_year + '-' + regexC_month + '-' + regexC_day
                                        # datetime will give a ValueError if not a good date or a param is not int
                                        date1 = datetime.date(int(regexC_year), int(regexC_month), int(regexC_day))
                                    except:
                                        logger.debug('Magazine %s not in proper date format.' % nzbtitle_formatted)
                                        bad_date = bad_date + 1
                                        # allow issues with good name but bad date to be included
                                        # so user can manually select them, incl those with issue numbers
                                        newdatish = "1970-01-01"  # provide a fake date for bad-date issues
                                        # continue
                        else:
                            continue

                        #  store all the _new_ matching results, mark as "skipped" for now
                        #  we change the status to "wanted" on the ones we want to snatch later
                        #  don't add a new entry if this issue has been found on an earlier search
                        mag_entry = myDB.select('SELECT * from wanted WHERE NZBurl="%s"' % nzburl)
                        if not mag_entry:
                            controlValueDict = {"NZBurl": nzburl}
                            newValueDict = {
                                "NZBprov": nzbprov,
                                "BookID": bookid,
                                "NZBdate": nzbdate,
                                "NZBtitle": nzbtitle,
                                "AuxInfo": newdatish,
                                "Status": "Skipped",
                                "NZBsize": nzbsize,
                                "NZBmode": nzbmode
                            }
                            myDB.upsert("wanted", newValueDict, controlValueDict)

                        if control_date is None:  # we haven't got any copies of this magazine yet
                            # get a rough time just over a month ago to compare to, in format yyyy-mm-dd
                            # could perhaps calc differently for weekly, biweekly etc
                            start_time = time.time()
                            start_time -= 31 * 24 * 60 * 60  # number of seconds in 31 days
                            control_date = time.strftime("%Y-%m-%d", time.localtime(start_time))

                        # only grab a copy if it's newer than the most recent we have,
                        # or newer than a month ago if we have none
                        comp_date = formatter.datecompare(newdatish, control_date)
                        if comp_date > 0:
                            # Should probably only upsert when downloaded and processed in case snatch fails
                            # keep track of what we're going to download so we don't download dupes
                            new_date = new_date + 1
                            issue = bookid + ',' + newdatish
                            if issue not in issues:
                                maglist.append({
                                    'bookid': bookid,
                                    'nzbprov': nzbprov,
                                    'nzbtitle': nzbtitle,
                                    'nzburl': nzburl,
                                    'nzbmode': nzbmode
                                })
                                logger.debug('This issue of %s is new, downloading' % nzbtitle_formatted)
                                to_snatch = to_snatch + 1
                                issues.append(issue)

                                controlValueDict = {"NZBurl": nzburl}
                                newValueDict = {
                                    "NZBdate": formatter.now(),  # when we asked for it
                                    "Status": "Wanted"
                                }
                                myDB.upsert("wanted", newValueDict, controlValueDict)

                            else:
                                logger.debug('This issue of %s is already flagged for download' % issue)
                        else:
                            if newdatish != "1970-01-01":  # this is our fake date for ones we can't decipher
                                logger.debug('This issue of %s is old; skipping.' % nzbtitle_formatted)
                                old_date = old_date + 1
                    else:
                        logger.debug('Magazine [%s] does not completely match search term [%s].' % (
                                     nzbtitle_formatted, bookid))
                        bad_regex = bad_regex + 1

            logger.info('Found %i results for %s. %i new, %i old, %i fail date, %i fail name: %i to download' % (
                        total_nzbs, bookid, new_date, old_date, bad_date, bad_regex, to_snatch))

            for items in maglist:
                if items['nzbmode'] == "torznab":
                    snatch = TORDownloadMethod(items['bookid'], items['nzbprov'], items['nzbtitle'], items['nzburl'])
                elif items['nzbmode'] == "torrent":
                    snatch = TORDownloadMethod(items['bookid'], items['nzbprov'], items['nzbtitle'], items['nzburl'])
                else:
                    snatch = NZBDownloadMethod(items['bookid'], items['nzbprov'], items['nzbtitle'], items['nzburl'])
                if snatch:
                    notifiers.notify_snatch(formatter.latinToAscii(items['nzbtitle']) + ' at ' + formatter.now())
                    common.schedule_job(action='Start', target='processDir')
            maglist = []

    if reset:
        common.schedule_job(action='Restart', target='search_magazines')

    logger.info("Search for magazines complete")
