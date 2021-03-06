"""
ServerPrimitives.py (c) 2003-2006 Alen Peacock.  This program is distributed
under the terms of the GNU General Public License (the GPL), version 3.

Primitive server storage protocol
"""

import binascii, time, os, stat, httplib, gc, re, sys, logging, sets
import tempfile, tarfile
from StringIO import StringIO
from twisted.web.resource import Resource
from twisted.web import server, resource, client
from twisted.internet import reactor, threads, defer
from twisted.web import http
from twisted.python import failure

from flud.FludCrypto import FludRSA, hashstring, hashfile, generateRandom
import flud.TarfileUtils as TarfileUtils
from flud.fencode import fencode, fdecode

import BlockFile
from FludCommUtil import *

logger = logging.getLogger("flud.server.op")
loggerid = logging.getLogger("flud.server.op.id")
loggerstor = logging.getLogger("flud.server.op.stor")
loggerretr = logging.getLogger("flud.server.op.rtrv")
loggervrfy = logging.getLogger("flud.server.op.vrfy")
loggerdele = logging.getLogger("flud.server.op.dele")
loggerauth = logging.getLogger("flud.server.op.auth")

"""
These classes represent http requests received by this node, and the actions
taken to respond.
"""

# FUTURE: check flud protocol version for backwards compatibility
# XXX: need to refactor challengeResponse stuff so that all share this same
#      code (put it in REQUEST obj).  See if we can do the same for some of
#      the other bits.
# XXX: need to make sure we have appropriate timeouts for all comms.
# FUTURE: DOS attacks.  For now, assume that network hardware can filter these 
#      out (by throttling individual IPs) -- i.e., it isn't our problem.  If we
#      want to defend against this at some point, we need to keep track of who
#      is generating requests and then ignore them.
# XXX: find everywhere we are sending longs and consider sending hex (or our
#      own base-64) encoded instead
# XXX: might want to consider some self-healing for the kademlia layer, as 
#      outlined by this thread: 
#      http://zgp.org/pipermail/p2p-hackers/2003-August/001348.html (should
#      also consider Zooko's links in the parent to this post)
# XXX: can man-in-the-middle get us with http basic auth, i.e., a node (Bob)
#      receives a store request from node Alice.  Bob 'forwards' this request
#      to another node, Charlie, posing as Alice.  When the challenge comes
#      back from Charlie, Bob relays it back to Alice, gets Alice's response,
#      and uses that response to answer Charlie's challenge.  Bob then pretends
#      to store the data, while in reality the data is at Charlie (VERIFYs can
#      be performed similarly).
#        [This scenario appears legitimate, but it isn't problematic.  Bob can
#        only store at Charlie if Charlie trusts Bob.  And if Bob trust Charlie
#        so much that he is willing to stake his trust with Alice on Charlie's
#        reliability, there shouldn't be anything wrong with allowing this.  If
#        Charlie goes down, Bob will go down (partly) with him, as far as trust
#        is concerned].
#        [should disable/remove CHALLENGE and GROUPID so that this only works
#        for forwarding.  Leaving CHALLENGE/GROUPID allows imposters.  Limiting
#        only to forwarding imposters.]
# XXX: Auth problem still exists.  Http-auth allows servers to know that clients
#      legitimate, but clients don't know the legitimacy of servers.  Proposal:
#      all client requests are currently responded to with a challenge that is
#      copied in the header and body.  Instead, require that the server sign
#      the url it was handed by the client, and return this in the body
#      (challenge remains in the header).  Now, the client can verify that the
#      server is who it says it is.  This is still somewhat vulnerable to replay
#      (if the same url is repeated, which it can be), but this only matters if
#      the client sends the exact same message again later to an imposter
#      (imposters can only impersonate the server).  Alternatively, could send
#      a challenge as a param of the request. [I think this is fixed now?]
# XXX: disallow requests originating from self.


challengelength = 40  # XXX: is 40 bytes sufficient?

class ROOT(Resource):
    """
    Notes on parameters common to most requests:
    Ku_e - the public key RSA exponent (usually 65537L, but can be other values)
    Ku_n - the public key RSA modulus.
    port - the port that the reqestor runs their fludserver on.
    
    All REQs will contain a public key ('Ku_e', 'Ku_n') from which nodeID can
    be derived, and a requestID ('reqID').  For the ops that don't require
    authentication (currently ID and dht ops), it may be convenient to also
    require nodeID (in this case, it is a good idea to check nodeID against
    Ku). 
    
    Client Authentication works as follows: Each client request is responded to
    with a 401 response in the header, with a challenge in the header message
    and repeated in the body.  The client reissues the response, filling the
    user 'username' field with the challenge-response and the 'password' field
    with groupIDu.  The client must compute the response to the challenge each
    time by examining this value.  The groupIDu never changes, so the client
    can simply send this value back with the response.  This implies some
    server-side state; since the server responds with a 401 and expects the
    client to send a new request with an Authorization header that contains the
    challenge response, the server has to keep some state for each client.
    This state expires after a short time, and implies that client must make
    requests to individual servers serially.

    A node's groupIDu is the same globally for each peer, so exposing it to one
    adversarial node means exposing it to all.  At first glance, this seems
    bad, but groupIDu is useless by itself -- in order to use it, a node must
    also be able to answer challenges based on Kr.  In other words, the
    security of groupIDu in this setting depends on Kr.
    """
    # XXX: web2 will let us do cool streaming things someday

    def __init__(self, fludserver):
        """
        All children should inherit.  Make sure to call super.__init__ if you
        override __init__
        """
        Resource.__init__(self)
        self.fludserver = fludserver
        self.node = fludserver.node
        self.config = fludserver.node.config
    
    def getChild(self, name, request):
        """
        should override.
        """
        if name == "":
            return self
        return Resource.getChild(self, name, request)

    def render_GET(self, request):
        self.setHeaders(request)
        return "<html>Flud</hrml>"

    def setHeaders(self, request):
        request.setHeader('Server','FludServer 0.1')
        request.setHeader('FludProtocol', PROTOCOL_VERSION)


class ID(ROOT):
    """ self identification / kad ping """
    def getChild(self, name, request):
        return self

    def render_GET(self, request):
        """
        Just received a request to expose my identity.  Send public key (from
        which requestor can determine nodeID).
        Response codes: 200- OK (default)
                        204- No Content (returned in case of error or not
                             wanting to divulge ID)
                        400- Bad Request (missing params or bad requesting ID)
        """
        self.setHeaders(request)
        try:
            required = ('nodeID', 'Ku_e', 'Ku_n', 'port')
            params = requireParams(request, required)
        except Exception, inst:
            msg = "%s in request received by ID" % inst.args[0] 
            loggerid.info(msg)
            request.setResponseCode(http.BAD_REQUEST, "Bad Request")
            return msg 
        else:
            loggerid.info("received ID request from %s..." 
                    % params['nodeID'][:10])
            loggerid.info("returning ID response")
            #try:
            reqKu = {}
            reqKu['e'] = long(params['Ku_e'])
            reqKu['n'] = long(params['Ku_n'])
            reqKu = FludRSA.importPublicKey(reqKu)
            if reqKu.id() != params['nodeID']:
                request.setResponseCode(http.BAD_REQUEST, "Bad Identity")
                return "requesting node's ID and public key do not match"
            host = getCanonicalIP(request.getClientIP())
            updateNode(self.node.client, self.config, host,
                    int(params['port']), reqKu, params['nodeID'])
            return str(self.config.Ku.exportPublicKey())
            #except:
            #   msg = "can't return ID"
            #   loggerid.log(logging.WARN, msg)
            #   request.setResponseCode(http.NO_CONTENT, msg)
            #   return msg

class FILE(ROOT):
    """ data storage file operations: POST, GET, DELETE """
    def getChild(self, name, request):
        if len(request.prepath) != 2:
            return Resource.getChild(self, name, request)
        return self

    def render_POST(self, request):
        """
        A request to store data via http upload.  The file to delete is
        indicated by the URL path, e.g. 
        POST http://server:port/a35cd1339766ef209657a7b
        Response codes: 200- OK (default)
                        400- Bad Request (missing params) 
                        401- Unauthorized (ID hash, CHALLENGE, or 
                             GROUPCHALLENGE failed) 

        Each file fragment is stored with its storage key as the file name.
        The file fragment can be 'touched' (or use last access time if
        supported) each time it is verified or read, so that we have a way to
        record age (which also allows a purge strategy).  Files are
        reference-listed (reference count with owners) by the BlockFile object.
        """
        loggerstor.debug("file POST, %s", request.prepath)
        filekey = request.prepath[1]
        self.setHeaders(request)
        return StoreFile(self.node, self.config, request, filekey).deferred

    def render_GET(self, request):
        """
        A request to retrieve data.  The file to retrieve is indicated by the
        URL path, e.g. GET http://server:port/a35cd1339766ef209657a7b
        Response codes: 200- OK (default)
                        400- Bad Request (missing params) 
                        401- Unauthorized (ID hash, CHALLENGE, or 
                             GROUPCHALLENGE failed) 
        """
        loggerstor.debug("file GET, %s", request.prepath)
        filekey = request.prepath[1]
        self.setHeaders(request)
        return RetrieveFile(self.node, self.config, request, filekey).deferred

    def render_DELETE(self, request):
        """
        A request to delete data.  The file to delete is indicated by the URL
        path, e.g. DELETE http://server:port/a35cd1339766ef209657a7b
        Response codes: 200- OK (default)
                        400- Bad Request (missing params) 
                        401- Unauthorized (ID hash, CHALLENGE, or 
                            GROUPCHALLENGE failed, or nodeID doesn't own this
                            block) 
        """
        loggerstor.debug("file DELETE, %s", request.prepath)
        filekey = request.prepath[1]
        self.setHeaders(request)
        return DeleteFile(self.node, self.config, request, filekey).deferred

class HASH(ROOT):
    """ verification of data via challenge-response """
    def getChild(self, name, request):
        return self

    def render_GET(self, request):
        """
        Just received a storage VERIFY request.
        Response codes: 200- OK (default)
                        400- Bad Request (missing params) 
                        401- Unauthorized (ID hash, CHALLENGE, or 
                             GROUPCHALLENGE failed) 
        
        [VERIFY is the most important of the trust-building ops.  Issues:
          1) how often do we verify.
            a) each file
              A) each block of each file
          2) proxying hurts trust (see PROXY below)
        The answer to #1 is, ideally, we check every node who is storing for us
        every quanta.  But doing the accounting for keeping track of who is
        storing for us is heavy, so instead we just want to check all of our
        files and hope that gives sufficient coverage, on average, of the nodes
        we store to.  But we have to put some limits on this, and the limits
        can't be imposed by the sender (since a greedy sender can just modify
        this), so peer nodes have to do some throttling of these types of
        requests.  But such throttling is tricky, as the requestor must
        decrease trust when VERIFY ops fail.  Also, since we will just randomly
        select from our files, such a throttling scheme will reduce our
        accuracy as we store more and more data (we can only verify a smaller
        percentage).  Peers could enforce limits as a ratio of total data
        stored for a node, but then the peers could act maliciously by
        artificially lowering this number.  In summary, if we don't enforce
        limits, misbehaving nodes could flood VERIFY requests resulting in
        effecitve DOS attack.  If we do enforce limits, we have to watch out
        for trust wars where both nodes end up destroying all trust between
        them.  Possible answer: set an agreed-upon threshold a priori.  This
        could be a hardcoded limit, or (better) negotiated between node pairs.
        If the requestor goes over this limit, he should understand that his
        trust will be decreased by requestee.  If he doesn't understand this,
        his trust *should* be decreased, and if he decreases his own trust in
        us as well, we don't care -- he's misbehaving.]
        """
        loggervrfy.debug("file VERIFY, %s", request.prepath)
        if len(request.prepath) != 2:
            # XXX: add support for sending multiple verify ops in a single
            # request
            request.setResponseCode(http.BAD_REQUEST, "expected filekey")
            return "expected file/[filekey], got %s" % '/'.join(request.prepath)
        filekey = request.prepath[1]
        self.setHeaders(request)
        return VerifyFile(self.node, self.config, request, filekey).deferred

class StoreFile(object):
    def __init__(self, node, config, request, filekey):
        self.node = node
        self.config = config
        self.deferred = self.storeFile(request, filekey)

    def storeFile(self, request, filekey):
        try:
            required = ('size', 'Ku_e', 'Ku_n', 'port')
            params = requireParams(request, required)
        except Exception, inst:
            msg = inst.args[0] + " in request received by STORE" 
            loggerstor.info(msg)
            request.setResponseCode(http.BAD_REQUEST, "Bad Request")
            return msg 
        else:
            host = getCanonicalIP(request.getClientIP())
            port = int(params['port'])
            loggerstor.info("received STORE request from %s:%s", host, port)

            requestedSize = int(params['size'])
            reqKu = {}
            reqKu['e'] = long(params['Ku_e'])
            reqKu['n'] = long(params['Ku_n'])
            reqKu = FludRSA.importPublicKey(reqKu)
            nodeID = reqKu.id()

            #if requestedSize > 10:
            #   msg = "unwilling to enter into storage relationship"
            #   request.setResponseCode(http.PAYMENT_REQUIRED, msg) 
            #   return msg

            return authenticate(request, reqKu, host, port, 
                    self.node.client, self.config,
                    self._storeFile, request, filekey, reqKu, nodeID)

    def _storeFile(self, request, filekey, reqKu, nodeID):
        # [XXX: memory management is not happy here.  might want to look at
        # request.registerProducer().  Otherwise, might have to scrap
        # using the STORE(ROOT(RESOURCE)) deal in favor of
        # producer/consumer model for STORE ops
        # (http://itamarst.org/writings/OSCON03/twisted_internet-108.html).
        # Another option might include subclassing web.resource.Resource
        # and making this derive from that...  Or might be web.Site that
        # needs to be subclassed... Or maybe web.site.Request -
        # web.site.Request.process()?  Request seems doubly-bad: perhaps a
        # copy is made somewhere, because memory mushrooms to 2x big
        # upload, then goes back down to around 1x.
        # [update: This should be fixable in twisted.web2, but I am informed
        # that in the current version, there is no workaround]

        # get the data to a tmp file
        loggerstor.debug("writing store data to tmpfile")
        tmpfile = tempfile.mktemp(dir=self.config.storedir)

        tarball = os.path.join(self.config.storedir,reqKu.id()+".tar")

        # rename and/or prepend the data appropriately
        tmpTarMode = None
        if filekey[-4:] == ".tar":
            tmpfile = tmpfile+".tar"
            tmpTarMode = 'r'
            targetTar = tarball
        elif filekey[-7:] == ".tar.gz":
            tmpfile = tmpfile+".tar.gz"
            tmpTarMode = 'r:gz'
            targetTar = tarball+".gz"
        loggerstor.debug("tmpfile is %s" % tmpfile)

        # XXX: if the server supports both .tar and tar.gz, this is wrong; we'd
        # need to check *both* for already existing dudes instead of just
        # choosing one
        if os.path.exists(tarball+'.gz'):
            tarball = (tarball+'.gz', 'r:gz')
        elif os.path.exists(tarball):
            tarball = (tarball, 'r')
        else:
            tarball = None
        loggerstor.debug("tarball is %s" % str(tarball))

        data = request.args.get('filename')[0]  # XXX: file in mem! need web2.
        # XXX: bad blocking stuff here
        f = open(tmpfile, 'wb')
        f.write(data)
        f.close()
        ftype = os.popen('file %s' % tmpfile)
        loggerstor.debug("ftype of %s is %s" % (tmpfile, ftype.read()))
        ftype.close()

        if tmpTarMode:
            # client sent a tarball
            loggerstor.debug("about to chksum %s" % tmpfile)
            digests = TarfileUtils.verifyHashes(tmpfile, '.meta')
            loggerstor.debug("chksum returned %s" % digests)
            ftype = os.popen('file %s' % tmpfile)
            loggerstor.debug("ftype of %s is %s" % (tmpfile, ftype.read()))
            ftype.close()
            if not digests:
                msg = "Attempted to use non-CAS storage key(s) for" \
                        " STORE tarball"
                loggerstor.debug(msg)
                os.remove(tmpfile)
                request.setResponseCode(http.CONFLICT, msg) 
                return msg
            # XXX: add digests to a db of already stored files (for quick 
            # lookup)
            if tarball:
                tarname, tarnameMode = tarball
                loggerstor.debug("concatenating tarfiles %s and %s" 
                        % (tarname, tmpfile))
                f1 = tarfile.open(tarname, tarnameMode)
                f2 = tarfile.open(tmpfile, tmpTarMode)
                f1names = f1.getnames()
                f2names = f2.getnames()
                f1.close()
                f2.close()
                dupes = [f for f in f1names if f in f2names]
                TarfileUtils.delete(tmpfile, dupes)
                ftype = os.popen('file %s' % tarname)
                loggerstor.debug("ftype of %s is %s" % (tarname, ftype.read()))
                ftype.close()
                TarfileUtils.concatenate(tarname, tmpfile)
                ftype = os.popen('file %s' % tarname)
                loggerstor.debug("ftype of %s is %s" % (tarname, ftype.read()))
                ftype.close()
            else:
                loggerstor.debug("saving %s as tarfile %s" % (tmpfile, 
                    targetTar))
                os.rename(tmpfile, targetTar)
        else:
            # client sent regular file
            h = hashfile(tmpfile)
            if request.args.has_key('meta') and request.args.has_key('metakey'):
                metakey = request.args.get('metakey')[0]
                meta = request.args.get('meta')[0]  # XXX: file in mem! 
            else:
                metakey = None
                meta = None
            if fencode(long(h, 16)) != filekey:
                msg = "Attempted to use non-CAS storage key for STORE data "
                msg += "(%s != %s)" % (filekey, fencode(long(h, 16)))
                os.remove(tmpfile)
                request.setResponseCode(http.CONFLICT, msg) 
                return msg
            fname = os.path.join(self.config.storedir, filekey)
            if os.path.exists(fname):
                loggerstor.debug("adding metadata to %s" % fname)
                f = BlockFile.open(fname,'rb+')
                if not f.hasNode(nodeID):
                    f.addNode(int(nodeID,16), {metakey: meta})
                    f.close()
                os.remove(tmpfile)
            else:
                if os.path.exists(nodeID+".tar"):
                    # XXX: need to do something with metadata!
                    print "XXX: need to do something with metadata for tar!"
                    tarball = tarfile.open(tarname, 'r')
                    if fname in tarball.getnames():
                        loggerstor.debug("%s already stored in tarball" % fname)
                        # if the file is already in the corresponding tarball,
                        # update its timestamp and return success.
                        loggerstor.debug("%s already stored" % filekey)
                        # XXX: update timestamp for filekey in tarball
                        return "Successful STORE"
                    else:
                        loggerstor.debug("tarball for %s, but %s not in tarball"
                                % (nodeID,fname))
                if len(data) < 8192 and fname != tarname: #XXX: magic # (blk sz)
                    # If the file is small, move it into the appropriate
                    # tarball.  Note that this code is unlikely to ever be
                    # executed if the client is an official flud client, as
                    # they do the tarball aggregation thing already, and all
                    # tarballs will be > 8192.  This is, then, really just
                    # defensive coding -- clients aren't required to implement
                    # that tarball aggregation strategy.  And it is really only
                    # useful for filesystems with inefficient small file 
                    # storage.
                    loggerstor.debug("moving small file '%s' into tarball" 
                            % fname)
                    if not os.path.exists(tarname):
                        tarball = tarfile.open(tarname, 'w')
                    else:
                        tarball = tarfile.open(tarname, 'a')
                    # XXX: more bad blocking stuff
                    tarball.add(tmpfile, os.path.basename(fname))
                    if meta:
                        metafilename = "%s.%s.meta" % (os.path.basename(fname), 
                                metakey)
                        loggerstor.debug("adding metadata file to tarball %s" 
                                % metafilename)
                        metaio = StringIO(meta)
                        tinfo = tarfile.TarInfo(metafilename)
                        tinfo.size = len(meta)
                        tarball.addfile(tinfo, metaio)
                    tarball.close()
                    os.remove(tmpfile)
                else:
                    # store the file 
                    loggerstor.debug("storing %s" % fname)
                    os.rename(tmpfile, fname)
                    BlockFile.convert(fname, (int(nodeID,16), {metakey: meta}))

        loggerstor.debug("successful STORE for %s" % filekey)
        return "Successful STORE"
    
    def _storeErr(self, error, request, msg):
        out = msg+": "+error.getErrorMessage()
        print "%s" % str(error)
        loggerstor.info(out)
        # update trust, routing
        request.setResponseCode(http.UNAUTHORIZED, 
                "Unauthorized: %s" % msg) # XXX: wrong code
        return msg

    def render_GET(self, request):
        request.setResponseCode(http.BAD_REQUEST, "Bad Request")
        return "STORE request must be sent using POST"


class RetrieveFile(object):
    def __init__(self, node, config, request, filekey):
        self.node = node
        self.config = config
        self.deferred = self.retrieveFile(request, filekey)

    def retrieveFile(self, request, filekey):
        try:
            required = ('Ku_e', 'Ku_n', 'port')
            params = requireParams(request, required)
        except Exception, inst:
            msg = inst.args[0] + " in request received by RETRIEVE" 
            loggerretr.log(logging.INFO, msg)
            request.setResponseCode(http.BAD_REQUEST, "Bad Request")
            return msg 
        else:
            host = getCanonicalIP(request.getClientIP())
            port = int(params['port'])
            loggerretr.info("received RETRIEVE request for %s from %s:%s...",
                    request.path, host, port)
            reqKu = {}
            reqKu['e'] = long(params['Ku_e'])
            reqKu['n'] = long(params['Ku_n'])
            reqKu = FludRSA.importPublicKey(reqKu)
            if request.args.has_key('metakey'):
                returnMeta = request.args['metakey']
                if returnMeta == 'True':
                    returnMeta = True
            else:
                returnMeta = True

            return authenticate(request, reqKu, host, int(params['port']), 
                    self.node.client, self.config,
                    self._sendFile, request, filekey, reqKu, returnMeta)
            

    def _sendFile(self, request, filekey, reqKu, returnMeta):
        fname = os.path.join(self.config.storedir,filekey)
        loggerretr.debug("reading file data from %s" % fname)
        # XXX: make sure requestor owns the file? 
        if returnMeta:
            loggerretr.debug("returnMeta = %s" % returnMeta)
            request.setHeader('Content-type', 'Multipart/Related')
            rand_bound = binascii.hexlify(generateRandom(13))
            request.setHeader('boundary', rand_bound)
        if not os.path.exists(fname):
            # check for tarball for originator
            tarball = os.path.join(self.config.storedir,reqKu.id()+".tar")
            tarballs = []
            if os.path.exists(tarball+'.gz'):
                tarballs.append((tarball+'.gz', 'r:gz'))
            if os.path.exists(tarball):
                tarballs.append((tarball, 'r'))
            loggerretr.debug("tarballs = %s" % tarballs)
            # XXX: does this work? does it close both tarballs if both got
            # opened?
            for tarball, openmode in tarballs:
                tar = tarfile.open(tarball, openmode)
                try:
                    tinfo = tar.getmember(filekey)
                    returnedMeta = False
                    if returnMeta:
                        loggerretr.debug("tar returnMeta %s" % filekey)
                        try:
                            metas = [f for f in tar.getnames()
                                        if f[:len(filekey)] == filekey 
                                        and f[-4:] == 'meta']
                            loggerretr.debug("tar returnMetas=%s" % metas)
                            for m in metas:
                                minfo = tar.getmember(m)
                                H = []
                                H.append("--%s" % rand_bound)
                                H.append("Content-Type: "
                                        "Application/octet-stream")
                                H.append("Content-ID: %s" % m)
                                H.append("Content-Length: %d" % minfo.size)
                                H.append("")
                                H = '\r\n'.join(H)
                                request.write(H)
                                request.write('\r\n')
                                tarm = tar.extractfile(minfo)
                                loggerretr.debug("successful metadata"
                                    " RETRIEVE (from %s)" % tarball)
                                # XXX: bad blocking stuff
                                while 1:
                                    buf = tarm.read()
                                    if buf == "":
                                        break
                                    request.write(buf)
                                    request.write('\r\n')
                                tarm.close()

                            H = []
                            H.append("--%s" % rand_bound)
                            H.append("Content-Type: Application/octet-stream")
                            H.append("Content-ID: %s" % filekey)
                            H.append("Content-Length: %d" % tinfo.size)
                            H.append("")
                            H = '\r\n'.join(H)
                            request.write(H)
                            request.write('\r\n')
                            returnedMeta = True
                        except:
                            # couldn't find any metadata, just return normal
                            # file
                            loggerretr.debug("no metadata found")
                            pass
                    # XXX: bad blocking stuff
                    tarf = tar.extractfile(tinfo)
                    # XXX: update timestamp on tarf in tarball
                    loggerretr.debug("successful RETRIEVE (from %s)" 
                            % tarball)
                    # XXX: bad blocking stuff
                    while 1:
                        buf = tarf.read()
                        if buf == "":
                            break
                        request.write(buf)
                    tarf.close()
                    tar.close()
                    if returnedMeta:
                        T = []
                        T.append("")
                        T.append("--%s--" % rand_bound)
                        T.append("")
                        T = '\r\n'.join(T)
                        request.write(T)
                    return ""
                except:
                    tar.close()
            request.setResponseCode(http.NOT_FOUND, "Not found: %s" % filekey)
            request.write("Not found: %s" % filekey)
        else:
            f = BlockFile.open(fname,"rb")
            loggerretr.log(logging.INFO, "successful RETRIEVE for %s" % filekey)
            meta = f.meta(int(reqKu.id(),16))
            if returnMeta and meta:
                loggerretr.debug("returnMeta %s" % filekey)
                loggerretr.debug("returnMetas=%s" % meta)
                for m in meta:
                    H = []
                    H.append("--%s" % rand_bound)
                    H.append("Content-Type: Application/octet-stream")
                    H.append("Content-ID: %s.%s.meta" % (filekey, m))
                    H.append("Content-Length: %d" % len(meta[m]))
                    H.append("")
                    H.append(meta[m])
                    H = '\r\n'.join(H)
                    request.write(H)
                    request.write('\r\n')

                H = []
                H.append("--%s" % rand_bound)
                H.append("Content-Type: Application/octet-stream")
                H.append("Content-ID: %s" % filekey)
                H.append("Content-Length: %d" % f.size())
                H.append("")
                H = '\r\n'.join(H)
                request.write(H)
                request.write('\r\n')
            # XXX: bad blocking stuff
            while 1:
                buf = f.read()
                if buf == "":
                    break
                request.write(buf)
            f.close()
            if returnMeta and meta:
                T = []
                T.append("")
                T.append("--%s--" % rand_bound)
                T.append("")
                request.write('\r\n'.join(T))
            return ""
    
    def _sendErr(self, error, request, msg):
        out = msg+": "+error.getErrorMessage()
        loggerretr.log(logging.INFO, out )
        # update trust, routing
        request.setResponseCode(http.UNAUTHORIZED, "Unauthorized: %s" % msg)
        request.write(msg)
        request.finish()


class VerifyFile(object):
    def __init__(self, node, config, request, filekey):
        self.node = node
        self.config = config
        self.deferred = self.verifyFile(request, filekey)

    isLeaf = True
    def verifyFile(self, request, filekey):
        """
        A VERIFY contains a file[fragment]id, an offset, and a length.  
        When this message is received, the given file[fragment] should be
        accessed and its bytes, from offset to offset+length, should be
        sha256 hashed, and the result returned.

        [in theory, we respond to all VERIFY requests.  In practice, however,
        we should probably do some throttling of responses to prevent DOS
        attacks and probing.  Or, maybe require that the sender encrypt the
        challenge with their private key...]

        [should also enforce some idea of reasonableness on length of bytes
        to verify]
        """
        try:
            required = ('Ku_e', 'Ku_n', 'port', 'offset', 'length')
            params = requireParams(request, required)
        except Exception, inst:
            msg = inst.args[0] + " in request received by VERIFY" 
            loggervrfy.log(logging.INFO, msg)
            request.setResponseCode(http.BAD_REQUEST, "Bad Request")
            loggervrfy.debug("BAD REQUEST")
            return msg 
        else:
            host = getCanonicalIP(request.getClientIP())
            port = int(params['port'])
            loggervrfy.log(logging.INFO,
                    "received VERIFY request from %s:%s...", host, port)
            if 'meta' in request.args:
                params['metakey'] = request.args['metakey'][0]
                params['meta'] = fdecode(request.args['meta'][0])
                loggerretr.info("VERIFY contained meta field with %d chars"
                        % len(params['meta']))
                meta = (params['metakey'], params['meta'])
            else:
                meta = None

            offset = int(params['offset'])
            length = int(params['length'])
            paths = [p for p in filekey.split(os.path.sep) if p != '']
            if len(paths) > 1:
                msg = "Bad request:"\
                        " filekey contains illegal path seperator tokens."
                loggerretr.debug(msg)
                request.setResponseCode(http.BAD_REQUEST, msg)
                return msg
            reqKu = {}
            reqKu['e'] = long(params['Ku_e'])
            reqKu['n'] = long(params['Ku_n'])
            reqKu = FludRSA.importPublicKey(reqKu)
            nodeID = reqKu.id()

            return authenticate(request, reqKu, host, port, 
                    self.node.client, self.config,
                    self._sendVerify, request, filekey, offset, length, reqKu,
                    nodeID, meta)
            
                        
    def _sendVerify(self, request, filekey, offset, length, reqKu, nodeID, 
            meta):
        fname = os.path.join(self.config.storedir,filekey)
        loggervrfy.debug("request for %s" % fname)
        if os.path.exists(fname):
            loggervrfy.debug("looking in regular blockfile for %s" % fname)
            if meta:
                f = BlockFile.open(fname, 'rb+')
            else:
                f = BlockFile.open(fname, 'rb')
        else:
            # check for tarball for originator
            loggervrfy.debug("checking tarball for %s" % fname)
            tarballs = []
            tarballbase = os.path.join(self.config.storedir, reqKu.id())+".tar"
            if os.path.exists(tarballbase+".gz"):
                tarballs.append((tarballbase+".gz", 'r:gz'))
            if os.path.exists(tarballbase):
                tarballs.append((tarballbase, 'r'))
            loggervrfy.debug("tarballs is %s" % tarballs)
            for tarball, openmode in tarballs:
                loggervrfy.debug("looking in tarball %s..." % tarball)
                tar = tarfile.open(tarball, openmode)
                try:
                    tarf = tar.extractfile(filekey)
                    tari = tar.getmember(filekey)
                    # XXX: update timestamp on tarf in tarball
                    fsize = tari.size
                    if offset > fsize or (offset+length) > fsize:
                        # XXX: should limit length
                        loggervrfy.debug("VERIFY response failed (from %s):"
                                " bad offset/length" % tarball)
                        msg = "Bad request: bad offset/length in VERIFY"
                        request.setResponseCode(http.BAD_REQUEST, msg) 
                        return msg
                    # XXX: could avoid seek/read if length == 0
                    tarf.seek(offset)
                    # XXX: bad blocking read
                    data = tarf.read(length)
                    tarf.close()
                    if meta:
                        mfname = "%s.%s.meta" % (filekey, meta[0])
                        loggervrfy.debug("looking for %s" % mfname)
                        if mfname in tar.getnames():
                            # make sure that the data is the same, if not,
                            # remove it and re-add it
                            tarmf = tar.extractfile(mfname)
                            # XXX: bad blocking read
                            stored_meta = tarmf.read()
                            tarmf.close()
                            if meta[1] != stored_meta:
                                loggervrfy.debug("updating tarball"
                                        " metadata for %s.%s" 
                                        % (filekey, meta[0]))
                                tar.close()
                                TarfileUtils.delete(tarball, mfname)
                                if openmode == 'r:gz':
                                    tarball = TarfileUtils.gunzipTarball(
                                            tarball)
                                tar = tarfile.open(tarball, 'a')
                                metaio = StringIO(meta[1])
                                tinfo = tarfile.TarInfo(mfname)
                                tinfo.size = len(meta[1])
                                tar.addfile(tinfo, metaio)
                                tar.close()
                                if openmode == 'r:gz':
                                    tarball = TarfileUtils.gzipTarball(
                                            tarball)
                            else:
                                loggervrfy.debug("no need to update tarball"
                                        " metadata for %s.%s" 
                                        % (filekey, meta[0]))
                        else:
                            # add it
                            loggervrfy.debug("adding tarball metadata"
                                    " for %s.%s" % (filekey, meta[0]))
                            tar.close()
                            if openmode == 'r:gz':
                                tarball = TarfileUtils.gunzipTarball(tarball)
                            tar = tarfile.open(tarball, 'a')
                            metaio = StringIO(meta[1])
                            tinfo = tarfile.TarInfo(mfname)
                            tinfo.size = len(meta[1])
                            tar.addfile(tinfo, metaio)
                            tar.close()
                            if openmode == 'r:gz':
                                tarball = TarfileUtils.gzipTarball(
                                        tarball)
                        
                    tar.close()
                    hash = hashstring(data)
                    loggervrfy.info("successful VERIFY (from %s)" % tarball)
                    return hash
                except:
                    tar.close()
            loggervrfy.debug("requested file %s doesn't exist" % fname)
            msg = "Not found: not storing %s" % filekey
            request.setResponseCode(http.NOT_FOUND, msg)
            return msg

        # make sure request is reasonable   
        fsize = os.stat(fname)[stat.ST_SIZE]
        if offset > fsize or (offset+length) > fsize:
            # XXX: should limit length
            loggervrfy.debug("VERIFY response failed (bad offset/length)")
            msg = "Bad request: bad offset/length in VERIFY"
            request.setResponseCode(http.BAD_REQUEST, msg) 
            return msg
        else:
            # XXX: blocking
            # XXX: could avoid seek/read if length == 0 (noop for meta update)
            f.seek(offset)
            data = f.read(length)
            if meta:
                loggervrfy.debug("adding metadata for %s.%s" 
                        % (fname, meta[0]))
                f.addNode(int(nodeID, 16) , {meta[0]: meta[1]})
            # XXX: blocking
            f.close()
            hash = hashstring(data)
            loggervrfy.debug("returning VERIFY")
            return hash
    
    def _sendErr(self, error, request, msg):
        out = "%s:%s" % (msg, error.getErrorMessage())
        loggervrfy.info(out)
        # update trust, routing
        request.setResponseCode(http.UNAUTHORIZED, "Unauthorized: %s" % msg)
        request.write(msg)
        request.finish()


class DeleteFile(object):
    def __init__(self, node, config, request, filekey):
        self.node = node
        self.config = config
        self.deferred = self.deleteFile(request, filekey)

    def deleteFile(self, request, filekey):
        try:
            required = ('Ku_e', 'Ku_n', 'port', 'metakey')
            params = requireParams(request, required)
        except Exception, inst:
            msg = inst.args[0] + " in request received by DELETE" 
            loggerdele.log(logging.INFO, msg)
            request.setResponseCode(http.BAD_REQUEST, "Bad Request")
            return msg 
        else:
            host = getCanonicalIP(request.getClientIP())
            port = int(params['port'])
            loggerdele.debug("received DELETE request for %s from %s:%s"
                    % (request.path, host, port))

            reqKu = {}
            reqKu['e'] = long(params['Ku_e'])
            reqKu['n'] = long(params['Ku_n'])
            reqKu = FludRSA.importPublicKey(reqKu)
            nodeID = reqKu.id()
            metakey = params['metakey']

            return authenticate(request, reqKu, host, port, 
                    self.node.client, self.config,
                    self._deleteFile, request, filekey, metakey, reqKu, nodeID)

    def _deleteFile(self, request, filekey, metakey, reqKu, reqID):
        fname = os.path.join(self.config.storedir, filekey)
        loggerdele.debug("reading file data from %s" % fname)
        if not os.path.exists(fname):
            # check for tarball for originator
            tarballs = []
            tarballbase = os.path.join(self.config.storedir, reqKu.id())+".tar"
            if os.path.exists(tarballbase+".gz"):
                tarballs.append((tarballbase+".gz", 'r:gz'))
            if os.path.exists(tarballbase):
                tarballs.append((tarballbase, 'r'))
            for tarball, openmode in tarballs:
                mfilekey = "%s.%s.meta" % (filekey, metakey)
                loggerdele.debug("opening %s, %s for delete..." 
                        % (tarball, openmode))
                ftype = os.popen('file %s' % tarball)
                loggerdele.debug("ftype of %s is %s" % (tarball, ftype.read()))
                ftype.close()
                tar = tarfile.open(tarball, openmode)
                mnames = [n for n in tar.getnames() 
                        if n[:len(filekey)] == filekey]
                tar.close()
                if len(mnames) > 2:
                    deleted = TarfileUtils.delete(tarball, mfilekey)
                else:
                    deleted = TarfileUtils.delete(tarball, [filekey, mfilekey])
                if deleted:
                    loggerdele.info("DELETED %s (from %s)" % (deleted, tarball))
                return ""
            request.setResponseCode(http.NOT_FOUND, "Not found: %s" % filekey)
            request.write("Not found: %s" % filekey)
        else:
            f = BlockFile.open(fname,"rb+")
            nID = int(reqID, 16)
            if f.hasNode(nID):
                # remove this node/metakey from owning this file block
                f.delNode(nID, metakey)
                if f.emptyNodes():
                    # if this was the only owning node, delete it
                    f.close()
                    os.remove(fname)
            f.close()
            loggerdele.debug("returning DELETE response")
            return ""
    
    def _sendErr(self, error, request, msg):
        out = msg+": "+error.getErrorMessage()
        loggerdele.log(logging.INFO, out )
        # update trust, routing
        request.setResponseCode(http.UNAUTHORIZED, "Unauthorized: %s" % msg)
        request.write(msg)
        request.finish()


class PROXY(ROOT):
    """
    This is a special request which wraps another request.  If received,
    the node is required to forward the request and keep state to re-wrap when
    the response returns.
    This is useful for several reasons:
      1) insert a bit of anonymity into VERIFY ops, so that a malicious node
         can't know to do an early purge data for a missing node.
      2) NAT'd boxes will need to use relays for STORE, VERIFY, and RETRIEVE
         (since all those ops cause the receive to make a connection back)
    [There must be an incentive for nodes to offer PROXY (trust), otherwise
    it is advantageous to turn it off.]
    [disadvantages of proxy: it hurts trust.  How do we know that a bad (trust 
    decreasing) op really was caused by the originator?  Couldn't the failure
    be caused by one of the proxies?]
    [Should consider using GnuNet's "Excess-Based Economic Model" where each
    request contains a priority which 'spends' some of the trust at the 
    requestee when resources are scarce.  In this model, the proxying node[s]
    charge a fee on the priority, reducing it by a small amount as they
    forward the request.]
    """
    # XXX: needs to be RESTified when implemented

    isLeaf = True
    def render_GET(self, request):
        self.setHeaders(request)
        result = "NOT YET IMPLEMENTED"
        return result
    
def authenticate(request, reqKu, host, port, client, config, callable, 
        *callargs):
    # 1- make sure that reqKu hashes to reqID
    # 2- send a challenge/groupchallenge to reqID (encrypt with reqKu)
    challengeResponse = request.getUser()
    groupResponse = request.getPassword()

    if not challengeResponse or not groupResponse:
        loggerauth.info("returning challenge for request from %s:%d" \
                % (host, port))
        return sendChallenge(request, reqKu, config.nodeID)
    else:
        if getChallenge(challengeResponse):
            expireChallenge(challengeResponse)
            if groupResponse == hashstring(
                    str(reqKu.exportPublicKey())
                    +str(config.groupIDr)): 
                updateNode(client, config, host, port, reqKu, reqKu.id())
                return callable(*callargs)
            else:
                err = "Group Challenge Failed"
                loggerauth.info(err)
                loggerauth.debug("Group Challenge received was %s" \
                        % groupResponse)
                # XXX: update trust, routing
                request.setResponseCode(http.FORBIDDEN, err);
                return err
        else:
            err = "Challenge Failed"
            loggerauth.info(err)
            loggerauth.debug("Challenge received was %s" % challengeResponse)
            # XXX: update trust, routing
            request.setResponseCode(http.FORBIDDEN, err);
            return err


def sendChallenge(request, reqKu, id):
    challenge = generateRandom(challengelength) 
    while challenge[0] == '\x00':
        # make sure we have at least challengelength bytes
        challenge = generateRandom(challengelength)
    addChallenge(challenge)
    loggerauth.debug("unencrypted challenge is %s" 
            % fencode(binascii.unhexlify(id)+challenge))
    echallenge = reqKu.encrypt(binascii.unhexlify(id)+challenge)[0]
    echallenge = fencode(echallenge)
    loggerauth.debug("echallenge = %s" % echallenge)
    # since challenges will result in a new req/resp pair being generated,
    # these could take much longer than the primitive_to.  Expire in
    # 15*primitive_to
    reactor.callLater(primitive_to*15, expireChallenge, challenge, True)
    resp = 'challenge = %s' % echallenge
    loggerauth.debug("resp = %s" % resp)
    request.setResponseCode(http.UNAUTHORIZED, echallenge)
    request.setHeader('Connection', 'close')
    request.setHeader('WWW-Authenticate', 'Basic realm="%s"' % 'default')
    request.setHeader('Content-Length', str(len(resp)))
    request.setHeader('Content-Type', 'text/html')
    request.setHeader('Pragma','claimreserve=5555')  # XXX: this doesn't work
    return resp

outstandingChallenges = {}
def addChallenge(challenge):
    outstandingChallenges[challenge] = True
    loggerauth.debug("added challenge %s" % fencode(challenge))

def expireChallenge(challenge, expired=False):
    try:
        challenge = fdecode(challenge)
    except:
        pass
    if outstandingChallenges.has_key(challenge):
        del outstandingChallenges[challenge]
        if expired:
            # XXX: should put these in an expired challenge list so that we
            #      can send a more useful message on failure (must then do
            #      expirations on expired list -- maybe better to just make
            #      these expirations really liberal).
            loggerauth.debug("expired challenge %s" % fencode(challenge))
        else:
            loggerauth.debug("deleted challenge %s" % fencode(challenge))

def getChallenge(challenge):
    try:
        challenge = fdecode(challenge)
    except:
        pass
    if outstandingChallenges.has_key(challenge):
        return outstandingChallenges[challenge]
    else:
        return None
