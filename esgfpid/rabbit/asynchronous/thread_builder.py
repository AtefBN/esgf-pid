import logging
import pika
import time
import copy
import datetime
from esgfpid.utils import get_now_utc_as_formatted_string as get_now_utc_as_formatted_string
import esgfpid.defaults as defaults
from esgfpid.utils import loginfo, logdebug, logtrace, logerror, logwarn, log_every_x_times
from ..exceptions import PIDServerException

LOGGER = logging.getLogger(__name__)
LOGGER.addHandler(logging.NullHandler())

'''

If the module fails connecting to a RabbitMQ node (on_connection_error),
or if the connection if interrupted (on_connection_closed),
it immediately tries connecting to the next RabbitMQ node.

If all hosts have been tried, the module starts over again, but waits some
seconds before that.

There is a maximum number of times that this is tried before
giving up Permanently.

'''
class ConnectionBuilder(object):
    
    def __init__(self, thread, statemachine, confirmer, returnhandler, shutter, nodemanager):
        self.thread = thread
        self.statemachine = statemachine

        '''
        We need to pass the "confirmer.on_delivery_confirmation()" callback to
        RabbitMQ's channel.'''
        self.confirmer = confirmer
        
        '''
        We need to pass the "returnhandler.on_message_not_accepted()"" callback
        to RabbitMQ's channel as "on_return_callback" '''
        self.returnhandler = returnhandler

        '''
        We need this to be able to trigger all the closing mechanisms 
        in case the module should close down as soon it was opened, i.e.
        if the close-command was issued while the connection was still
        building up.
        '''
        self.shutter = shutter

        '''
        The node manager keeps all the info about the RabbitMQ nodes,
        e.g. URLs, usernames, passwords.
        '''
        self.__node_manager = nodemanager

        '''
        To count how many times we have tried to reconnect the set of
        RabbitMQ hosts.
        '''
        self.__reconnect_counter = 0

        '''
        To see how many times we should try reconnecting to the set 
        of RabbitMQ hosts. Note that if there is 3 hosts, and we try 2
        times, this means 6 connection tries in total.
        '''
        self.__max_reconnection_tries = defaults.RABBIT_RECONNECTION_MAX_TRIES

        '''
        How many seconds to wait before reconnecting after having tried
        all hosts. (There is no waiting time trying to connect to a different
        host after one fails).
        '''
        self.__wait_seconds_before_reconnect = defaults.RABBIT_RECONNECTION_SECONDS

        '''
        Set of all tried hosts, for logging.
        '''
        self.__all_hosts_that_were_tried = set()

        '''
        To see how much time it takes to connect. Once a connection is
        established or failed, we print the time delta to logs.
        '''
        self.__start_connect_time = None

        '''
        Name of the fallback exchange to try if the normal exchange
        is not found.
        '''
        self.__fallback_exchange_name = defaults.RABBIT_FALLBACK_EXCHANGE_NAME

    ####################
    ### Start ioloop ###
    ####################

    '''
    Entry point. Called once to trigger the whole
    (re) connection process. Called from run method of the rabbit thread.
    '''
    def first_connection(self):
        logdebug(LOGGER, 'Trigger connection to rabbit...')
        self.__trigger_connection_to_rabbit_etc()
        logdebug(LOGGER, 'Trigger connection to rabbit... done.')
        logdebug(LOGGER, 'Start waiting for events...')
        self.__start_waiting_for_events()
        logtrace(LOGGER, 'Had started waiting for events, but stopped.')
    
    def __start_waiting_for_events(self):
        '''
        This waits until the whole chain of callback methods triggered by
        "trigger_connection_to_rabbit_etc()" has finished, and then starts 
        waiting for publications.
        This is done by starting the ioloop.

        Note: In the pika usage example, these things are both called inside the run()
        method, so I wonder if this check-and-wait here is necessary. Maybe not.
        But the usage example does not implement a Thread, so it probably blocks during
        the opening of the connection. Here, as it is a different thread, the run()
        might get called before the __init__ has finished? I'd rather stay on the
        safe side, as my experience of threading in Python is limited.
        '''

        # Start ioloop if connection object ready:
        if self.thread._connection is not None:
            try:
                logdebug(LOGGER, 'Starting ioloop...')
                logtrace(LOGGER, 'ioloop is owned by connection %s...', self.thread._connection)

                # Tell the main thread that we're now open for events.
                # As soon as the thread._connection object is not None anymore, it
                # can receive events.
                self.thread.tell_publisher_to_stop_waiting_for_thread_to_accept_events() 
                self.thread.continue_gently_closing_if_applicable()
                self.thread._connection.ioloop.start()

            except pika.exceptions.ProbableAuthenticationError as e:

                time_passed = datetime.datetime.now() - self.__start_connect_time
                logerror(LOGGER, 'Caught Authentication Exception after %s seconds during connection ("%s").', time_passed.total_seconds(), e.__class__.__name__)
                self.statemachine.set_to_waiting_to_be_available()
                self.statemachine.detail_authentication_exception = True # TODO WHAT FOR?

                # It seems that ProbableAuthenticationErrors do not cause
                # RabbitMQ to call any callback, either on_connection_closed
                # or on_connection_error - it just silently swallows the
                # problem.
                # So we need to manually trigger reconnection to the next
                # host here, which we do by manually calling the callback.
                errorname = 'ProbableAuthenticationError issued by pika'
                self.on_connection_error(self.thread._connection, errorname)

                # We start the ioloop, so it can handle the reconnection events,
                # or also receive events from the publisher in the meantime.
                self.thread._connection.ioloop.start()

            except Exception as e:
                # This catches any error during connection startup and during the entire
                # time the ioloop runs, blocks and waits for events.
                logerror(LOGGER, 'Unexpected error during event listener\'s lifetime: %s: %s', e.__class__.__name__, e.message)

                # As we will try to reconnect, set state to waiting to connect.
                # If reconnection fails, it will be set to permanently unavailable.
                self.statemachine.set_to_waiting_to_be_available()

                # In case this error is reached, it seems that no callback
                # was called that handles the problem. Let's try to reconnect
                # somewhere else.
                errorname = 'Unexpected error ('+str(e.__class__.__name__)+': '+str(e.message)+')'
                self.on_connection_error(self.thread._connection, errorname)

                # We start the ioloop, so it can handle the reconnection events,
                # or also receive events from the publisher in the meantime.
                self.thread._connection.ioloop.start()
        
        else:
            # I'm quite sure that this cannot happen, as the connection object
            # is created in "trigger_connection_...()" and thus exists, no matter
            # if the actual connection to RabbitMQ succeeded (yet) or not.
            logdebug(LOGGER, 'This cannot happen: Connection object is not ready.')
            logerror(LOGGER, 'Cannot happen. Cannot properly start the thread. Connection object is not ready.')

    ########################################
    ### Chain of callback functions that ###
    ### connect to rabbit                ###
    ########################################

    def __trigger_connection_to_rabbit_etc(self):
        self.statemachine.set_to_waiting_to_be_available()
        self.__please_open_connection()

    ''' Asynchronous, waits for answer from RabbitMQ.'''
    def __please_open_connection(self):
        params = self.__node_manager.get_connection_parameters()
        self.__start_connect_time = datetime.datetime.now()
        logdebug(LOGGER, 'Connecting to RabbitMQ at %s... (%s)',
            params.host, get_now_utc_as_formatted_string())
        self.__all_hosts_that_were_tried.add(params.host)
        loginfo(LOGGER, 'Opening connection to RabbitMQ...')
        self.thread._connection = pika.SelectConnection(
            parameters=params,
            on_open_callback=self.on_connection_open,
            on_open_error_callback=self.on_connection_error,
            on_close_callback=self.on_connection_closed,
            stop_ioloop_on_close=False # TODO Why not?
        )

    ''' Callback, called by RabbitMQ.'''
    def on_connection_open(self, unused_connection):
        logdebug(LOGGER, 'Opening connection... done.')
        loginfo(LOGGER, 'Connection to RabbitMQ at %s opened... (%s)',
            self.__node_manager.get_connection_parameters().host,
            get_now_utc_as_formatted_string())

        # Tell the main thread we're open for events now:
        # When the connection is open, the thread is ready to accept events.
        # Note: It was already ready when the connection object was created,
        # not just now that it's actually open. There was already a call to
        # "...stop_waiting..." in start_waiting_for_events(), which quite
        # certainly was carried out before this callback. So this call to
        # "...stop_waiting..." is likelily redundant!
        self.thread.tell_publisher_to_stop_waiting_for_thread_to_accept_events()
        self.__please_open_rabbit_channel()

    ''' Asynchronous, waits for answer from RabbitMQ.'''
    def __please_open_rabbit_channel(self):
        logdebug(LOGGER, 'Opening channel...')
        self.thread._connection.channel(on_open_callback=self.on_channel_open)

    ''' Callback, called by RabbitMQ. '''
    def on_channel_open(self, channel):
        time_passed = datetime.datetime.now() - self.__start_connect_time
        logdebug(LOGGER, 'Opening channel... done. Took %s seconds.' % time_passed.total_seconds())
        logtrace(LOGGER, 'Channel has number: %s.', channel.channel_number)
        self.thread._channel = channel
        self.__reconnect_counter = 0
        self.__add_on_channel_close_callback()
        self.__add_on_return_callback()
        self.__make_channel_confirm_delivery()
        self.__make_ready_for_publishing()

    def __make_channel_confirm_delivery(self):
        logtrace(LOGGER, 'Set confirm delivery... (Issue Confirm.Select RPC command)')
        self.thread._channel.confirm_delivery(callback=self.confirmer.on_delivery_confirmation)
        logdebug(LOGGER, 'Set confirm delivery... done.')
 
    def __make_ready_for_publishing(self):
        logdebug(LOGGER, '(Re)connection established, making ready for publication...')

        # Check for unexpected errors:
        if self.thread._channel is None:
            logerror(LOGGER, 'Channel is None after connecting to server. This should not happen.')
            self.statemachine.set_to_permanently_unavailable()
        if self.thread._connection is None:
            logerror(LOGGER, 'Connection is None after connecting to server. This should not happen.')
            self.statemachine.set_to_permanently_unavailable()

        # Normally, it should already be waiting to be available:
        if self.statemachine.is_WAITING_TO_BE_AVAILABLE():
            logdebug(LOGGER, 'Setup is finished. Publishing may start.')
            logtrace(LOGGER, 'Publishing will use channel no. %s!', self.thread._channel.channel_number)
            self.statemachine.set_to_available()
            self.__check_for_already_arrived_messages_and_publish_them()

        # It was asked to close in the meantime (but might be able to publish the last messages):
        elif self.statemachine.is_AVAILABLE_BUT_WANTS_TO_STOP():
            logdebug(LOGGER, 'Setup is finished, but the module was already asked to be closed in the meantime.')
            self.__check_for_already_arrived_messages_and_publish_them()

        # It was force-closed in the meantime:
        elif self.statemachine.is_PERMANENTLY_UNAVAILABLE(): # state was set in shutter module's __close_down()
            if self.statemachine.get_detail_closed_by_publisher():
                logdebug(LOGGER, 'Setup is finished now, but the module was already force-closed in the meantime.')
                self.shutter.safety_finish('closed before connection was ready. reclosing.')
            elif self.statemachine.detail_could_not_connect:
                logerror(LOGGER, 'This is not supposed to happen. If the connection failed, this part of the code should not be reached.')
            else:
                logerror(LOGGER, 'This is not supposed to happen. An unknown event set this module to be unavailable. When was this set to unavailable?')
        else:
            logdebug(LOGGER, 'Unexpected state.')

    def __check_for_already_arrived_messages_and_publish_them(self):
        logdebug(LOGGER, 'Checking if messages have arrived in the meantime...')
        num = self.thread.get_num_unpublished()
        if num > 0:
            loginfo(LOGGER, 'Ready to publish messages to RabbitMQ. %s messages are already waiting to be published.', num)
            for i in xrange(int(num*1.1)):
                self.thread.add_event_publish_message()
        else:
            loginfo(LOGGER, 'Ready to publish messages to RabbitMQ.')
            logdebug(LOGGER, 'Ready to publish messages to RabbitMQ. No messages waiting yet.')
        

    ########################
    ### Connection error ###
    ########################

    '''
    If the connection to RabbitMQ failed, there is various
    things that may happen:
    (1) If there is other RabbitMQ urls, it will try to connect 
        to one of these.
    (2) If there is no other URLs, it will try to reconnect to this
        one after a short waiting time.
    (3) If the maximum number of reconnection tries is reached, it
        gives up.
    '''
    def on_connection_error(self, connection, msg):

        oldhost = self.__node_manager.get_connection_parameters().host
        time_passed = datetime.datetime.now() - self.__start_connect_time
        loginfo(LOGGER, 'Failed connection to RabbitMQ at %s after %s seconds. Reason: %s.', oldhost, time_passed.total_seconds(), msg)

        # If there was a force-finish, we do not reconnect.
        if self.statemachine.is_FORCE_FINISHED():
            # TODO This is the same code as above. Make a give_up function from it?
            #self.statemachine.set_to_permanently_unavailable()
            #self.statemachine.detail_could_not_connect = True
            errormsg = ('Permanently failed to connect to RabbitMQ. Tried all hosts %s until received a force-finish. Giving up. No PID requests will be sent.' % list(self.__all_hosts_that_were_tried))
            logerror(LOGGER, errormsg)
            raise PIDServerException(errormsg)

        
        # If there is alternative URLs, try one of them:
        if self.__node_manager.has_more_urls():
            logdebug(LOGGER, 'Connection failure: %s fallback URLs left to try.', self.__node_manager.get_num_left_urls())
            self.__node_manager.set_next_host()
            newhost = self.__node_manager.get_connection_parameters().host
            loginfo(LOGGER, 'Connection failure: Trying to connect (now) to %s.', newhost)
            reopen_seconds = 0
            self.__wait_and_trigger_reconnection(connection, reopen_seconds)


        # If there is no URLs, reset the node manager to
        # start at the first nodes again...
        else:
            self.__reconnect_counter += 1;
            if self.__reconnect_counter <= self.__max_reconnection_tries:
                reopen_seconds = self.__wait_seconds_before_reconnect
                logdebug(LOGGER, 'Connection failure: Failed connecting to all hosts. Waiting %s seconds and starting over.', reopen_seconds)
                self.__node_manager.reset_nodes()
                newhost = self.__node_manager.get_connection_parameters().host
                loginfo(LOGGER, 'Connection failure: Trying to connect (in %s seconds) to %s.', reopen_seconds, newhost)
                self.__wait_and_trigger_reconnection(connection, reopen_seconds)

            # Give up after so many tries...
            else:
                self.statemachine.set_to_permanently_unavailable()
                self.statemachine.detail_could_not_connect = True
                errormsg = ('Permanently failed to connect to RabbitMQ. Tried all hosts %s %s times. Giving up. No PID requests will be sent.' % (list(self.__all_hosts_that_were_tried) ,self.__max_reconnection_tries))
                logerror(LOGGER, errormsg)
                raise PIDServerException(errormsg)


    #############################
    ### React to channel and  ###
    ### connection close      ###
    #############################

    ''' This tells RabbitMQ what to do if it receives 
    a message it cannot accept, e.g. if it cannot
    route it. '''
    def __add_on_return_callback(self):
        self.thread._channel.add_on_return_callback(self.returnhandler.on_message_not_accepted)

    '''
    This tells RabbitMQ what to do if the channel
    was closed.

    Note: Every connection close includes a channel close.
    However, as far as I know, this callback is only
    called if the channel is closed without the underlying
    connection being closed. I am not 100 percent sure though.
    '''
    def __add_on_channel_close_callback(self):
        self.thread._channel.add_on_close_callback(self.on_channel_closed)

    '''
    Callback, called by RabbitMQ.
    "on_channel_closed" can be called in three situations:

    (1) The user asked to close the connection.
        In this case, we want to clean up everything and leave it closed.

    (2) The connection was closed because we tried to publish to a non-
        existent exchange.
        In this case, the connection is still open, and we want to reopen
        a new channel and publish to a different exchange.
        We also want to republish the ones that had failed.

    (3) There was some problem that closed the connection, which causes
        the channel to close.
        In this case, we want to reopen a connection.

    '''
    def on_channel_closed(self, channel, reply_code, reply_text):
        logdebug(LOGGER, 'Channel was closed: %s (code %s)', reply_text, reply_code)

        # Channel closed because user wants to close:
        if self.statemachine.is_PERMANENTLY_UNAVAILABLE():
            if self.statemachine.get_detail_closed_by_publisher():
                logdebug(LOGGER,'Channel close event due to close command by user. This is expected.')

        # Channel closed because even fallback exchange did not exist:
        elif reply_code == 404 and "NOT_FOUND - no exchange 'FALLBACK'" in reply_text:
            logerror(LOGGER,'Channel closed because FALLBACK exchange does not exist. Need to close connection to trigger all the necessary close down steps.')
            self.thread._connection.close() # This will reconnect!

        # Channel closed because exchange did not exist:
        elif reply_code == 404:
            logdebug(LOGGER, 'Channel closed because the exchange "%s" did not exist.', self.__node_manager.get_exchange_name())
            self.__use_different_exchange_and_reopen_channel()

        # Other unexpected channel close:
        else:
            logerror(LOGGER,'Unexpected channel shutdown. Need to close connection to trigger all the necessary close down steps.')
            self.thread._connection.close() # This will reconnect!

    '''
    An attempt to publish to a nonexistent exchange will close
    the channel. In this case, we use a different exchange name
    and reopen the channel. The underlying connection was kept
    open.
    '''
    def __use_different_exchange_and_reopen_channel(self):

        # Set to waiting to be available, so that incoming
        # messages are stored:
        self.statemachine.set_to_waiting_to_be_available()

        # New exchange name
        logdebug(LOGGER, 'Setting exchange name to fallback exchange "%s"', self.__fallback_exchange_name)
        self.thread.set_exchange_name(self.__fallback_exchange_name)

        # If this happened while sending message to the wrong exchange, we
        # have to trigger their resending...
        self.__prepare_channel_reopen('Channel reopen')

        # Reopen channel
        # TODO Reihenfolge richtigen? Erst prepare, dann open?
        logdebug(LOGGER, 'Reopening channel...')
        self.statemachine.set_to_waiting_to_be_available()
        self.__please_open_rabbit_channel()

    '''
    Callback, called by RabbitMQ.
    "on_connection_closed" can be called in two situations:

    (1) The user asked to close the connection.
        In this case, we want to clean up everything and leave it closed.

    (2) There was some other problem that closed the connection.

    '''
    def on_connection_closed(self, connection, reply_code, reply_text):
        loginfo(LOGGER, 'Connection to RabbitMQ was closed. Reason: %s.', reply_text)
        self.thread._channel = None
        if self.__was_user_shutdown(reply_code, reply_text):
            loginfo(LOGGER, 'Connection to %s closed.', self.__node_manager.get_connection_parameters().host)
            self.make_permanently_closed_by_user()
        elif self.__was_permanent_error(reply_code, reply_text):
            loginfo(LOGGER, 'Connection to %s closed.', self.__node_manager.get_connection_parameters().host)
            self.make_permanently_closed_by_error(connection, reply_text)
        else:
            #reopen_seconds = defaults.RABBIT_RECONNECTION_SECONDS
            #self.__wait_and_trigger_reconnection(connection, reopen_seconds)
            self.on_connection_error(connection, reply_text)

    def __was_permanent_error(self, reply_code, reply_text):
        if self.thread.ERROR_TEXT_CONNECTION_PERMANENT_ERROR in reply_text:
            return True
        return False

    def __was_user_shutdown(self, reply_code, reply_text):
        if self.__was_forced_user_shutdown(reply_code, reply_text):
            return True
        elif self.__was_gentle_user_shutdown(reply_code, reply_text):
            return True
        return False

    def __was_forced_user_shutdown(self, reply_code, reply_text):
        if (reply_code==self.thread.ERROR_CODE_CONNECTION_CLOSED_BY_USER and
            self.thread.ERROR_TEXT_CONNECTION_FORCE_CLOSED in reply_text):
            return True
        return False

    def __was_gentle_user_shutdown(self, reply_code, reply_text):
        if (reply_code==self.thread.ERROR_CODE_CONNECTION_CLOSED_BY_USER and
            self.thread.ERROR_TEXT_CONNECTION_NORMAL_SHUTDOWN in reply_text):
            return True
        return False

    ''' Called by thread, by shutter module.'''
    def make_permanently_closed_by_user(self):
        # This changes the state of the state machine!
        # This needs to be called from the shutter module
        # in case there is a force_finish while the connection
        # is already closed (as the callback on_connection_closed
        # is not called then).
        self.statemachine.set_to_permanently_unavailable()
        logtrace(LOGGER, 'Stop waiting for events due to user interrupt!')
        logtrace(LOGGER, 'Permanent close: Stopping ioloop of connection %s...', self.thread._connection)
        self.thread._connection.ioloop.stop()
        loginfo(LOGGER, 'Stopped listening for RabbitMQ events (%s).', get_now_utc_as_formatted_string())
        logdebug(LOGGER, 'Connection to messaging service closed by user. Will not reopen.')

    def make_permanently_closed_by_error(self, connection, reply_text):
        # This changes the state of the state machine!
        # This needs to be called if there is a permanent
        # error and we don't want the library to reonnect,
        # and we also don't want to pretend it was closed
        # by the user.
        # This is really rarely needed. 
        self.statemachine.set_to_permanently_unavailable()
        logtrace(LOGGER, 'Stop waiting for events due to permanent error!')

        # In case the main thread was waiting for any synchronization event.
        self.thread.unblock_events()

        # Close ioloop, which blocks the thread.
        logdebug(LOGGER, 'Permanent close: Stopping ioloop of connection %s...', self.thread._connection)
        self.thread._connection.ioloop.stop()
        loginfo(LOGGER, 'Stopped listening for RabbitMQ events (%s).', get_now_utc_as_formatted_string())
        logdebug(LOGGER, 'Connection to messaging service closed because of error. Will not reopen. Reason: %s', reply_text)

    '''
    This triggers a reconnection to whatever host is stored in
    self.__node_manager.get_connection_parameters().host at the moment of reconnection.

    If it is called to reconnect to the same host, it is better
    to wait some seconds.

    If it is used to connect to the next host, there is no point
    in waiting.
    '''
    def __wait_and_trigger_reconnection(self, connection, wait_seconds):
        if self.statemachine.is_FORCE_FINISHED():
            # TODO This is the same code as above. Make a give_up function from it?
            #self.statemachine.set_to_permanently_unavailable()
            #self.statemachine.detail_could_not_connect = True
            #max_tries = defaults.RABBIT_RECONNECTION_MAX_TRIES
            errormsg = ('Permanently failed to connect to RabbitMQ. Tried all hosts %s until received a force-finish. Giving up. No PID requests will be sent.' % list(self.__all_hosts_that_were_tried))
            logerror(LOGGER, errormsg)
            raise PIDServerException(errormsg)
        else:
            self.statemachine.set_to_waiting_to_be_available()
            loginfo(LOGGER, 'Trying to reconnect to RabbitMQ in %s seconds.', wait_seconds)
            connection.add_timeout(wait_seconds, self.reconnect)
            logtrace(LOGGER, 'Reconnect event added to connection %s (not to %s)', connection, self.thread._connection)

    ###########################
    ### Reconnect after     ###
    ### unexpected shutdown ###
    ###########################

    '''
    Reconnecting creates a completely new connection.
    If we reconnect, we need to reset message number,
    delivery tag etc.

    We need to prepare to republish the yet-unconfirmed
    messages.

    Then we need to stop the old connection's ioloop.
    The reconnection will create a new connection object
    and this will have its own ioloop.

    '''
    def reconnect(self):
        logdebug(LOGGER, 'Reconnecting...')

        # We need to reset delivery tags, unconfirmed messages,
        # republish the unconfirmed, ...
        self.__prepare_channel_reopen('Reconnect')
        
        # This is the old connection ioloop instance, stop its ioloop
        logdebug(LOGGER, 'Reconnect: Stopping ioloop of connection %s...', self.thread._connection)
        self.thread._connection.ioloop.stop()
        # Note: All events still waiting on the ioloop are lost.
        # Messages are kept track of in the Queue.Queue or in the confirmer
        # module. Closing events are kept track on in shutter module.

        # Now we trigger the actual reconnection, which
        # works just like the first connection to RabbitMQ.
        self.first_connection()

    '''
    This is called during reconnection and during channel reopen.
    Both implies that a new channel is opened.
    '''
    def __prepare_channel_reopen(self, operation_string):
        # We need to reset the message number, as
        # it works by channel:
        logdebug(LOGGER, operation_string+': Resetting delivery number (for publishing messages).')
        self.thread.reset_delivery_number()

        # Furthermore, as we'd like to re-publish messages
        # that had not been confirmed yet, we remove them
        # from the stack of unconfirmed messages, and put them
        # back to the stack of unpublished messages.
        logdebug(LOGGER, operation_string+': Sending all messages that have not been confirmed yet...')
        self.__prepare_republication_of_unconfirmed()

        # Reset the unconfirmed delivery tags, as they also work by channel:
        logdebug(LOGGER, operation_string+': Resetting delivery tags (for confirming messages).')
        self.thread.reset_unconfirmed_messages_and_delivery_tags()
        
    def __prepare_republication_of_unconfirmed(self):
        # Get all unconfirmed messages - we won't be able to receive their confirms anymore:
        # IMPORTANT: This has to happen before we reset the delivery_tags of the confirmer
        # module, as this deletes the collection of unconfirmed messages.
        rescued_messages = self.thread.get_unconfirmed_messages_as_list_copy_during_lifetime()
        if len(rescued_messages)>0:
            logdebug(LOGGER, '%s unconfirmed messages were saved and are sent now.', len(rescued_messages))
            self.thread.send_many_messages(rescued_messages)
            # Note: The actual publish of these messages to rabbit
            # happens when the connection is there again, so no wrong delivery
            # tags etc. are created by this line!

