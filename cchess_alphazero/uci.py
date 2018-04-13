import sys
import os
import numpy as np
import multiprocessing as mp

from logging import getLogger
from collections import defaultdict
from threading import Thread
from threading import Timer
from time import sleep
from time import time

_PATH_ = os.path.dirname(os.path.dirname(__file__))

if _PATH_ not in sys.path:
    sys.path.append(_PATH_)

from cchess_alphazero.config import Config, PlayWithHumanConfig
from cchess_alphazero.lib.logger import setup_file_logger, setup_logger

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
config_type = 'distribute'
config = Config(config_type=config_type)
config.opts.device_list = '0'
config.resource.create_directories()
setup_file_logger(config.resource.play_log_path)
sys.stderr = open(config.resource.play_log_path, 'a')

import cchess_alphazero.environment.static_env as senv
from cchess_alphazero.agent.model import CChessModel
from cchess_alphazero.agent.player import CChessPlayer, VisitState
from cchess_alphazero.environment.lookup_tables import Winner, ActionLabelsRed, flip_move
from cchess_alphazero.lib.model_helper import load_best_model_weight
from cchess_alphazero.lib.tf_util import set_session_config

logger = getLogger(__name__)

CMD_LIST = ['uci', 'setoption', 'isrealy', 'position', 'go', 'stop', 'ponderhit', 'quit', 'fen', 'ucci']

class UCI:
    def __init__(self, config: Config):
        self.config = config
        self.args = None
        self.state = None
        self.is_red_turn = None
        self.player = None
        self.model = None
        self.pipe = None
        self.is_ready = False
        self.search_tree = None
        self.remain_time = None
        self.history = None
        self.turns = 0
        self.start_time = None
        self.end_time = None
        self.search_tree = None

    def main(self):
        while True:
            cmd = input()
            logger.debug(f"CMD: {cmd}")
            cmds = cmd.split(' ')
            self.args = cmds[1:]
            method = getattr(self, 'cmd_' + cmds[0], None)
            if method != None:
                method()
            else:
                logger.error(f"Error command: {cmd}")

    def cmd_uci(self):
        self.load_model()
        self.is_ready = True
        self.turns = 0
        self.remain_time = None
        self.state = senv.INIT_STATE
        self.history = [self.state]
        self.is_red_turn = True
        print('id name AlphaZero')
        print('uciok')

    def cmd_ucci(self):
        self.load_model()
        self.is_ready = True
        self.turns = 0
        self.remain_time = None
        self.state = senv.INIT_STATE
        self.history = [self.state]
        self.is_red_turn = True
        print('id name AlphaZero')
        print('ucciok')

    def cmd_setoption(self):
        '''
        setoption name <id> [value <x>]
        '''
        pass

    def cmd_isready(self):
        if self.is_ready == True:
            print('readyok')
        logger.debug(f"is_ready = {self.is_ready}")

    def cmd_position(self):
        '''
        position {fen <fenstring> | startpos } [moves <move1> .... <moven>]
        '''
        if not self.is_ready:
            return
        move_idx = -1
        if len(self.args) > 0:
            if self.args[0] == 'fen':
                # init with fen string
                fen = self.args[1]
                try:
                    self.state = senv.fen_to_state(fen)
                except Exception as e:
                    logger.error(f"cmd position error! cmd = {self.args}, {e}")
                    return
                self.history = [self.state]
                turn = self.args[2]
                if turn == 'b':
                    self.state = senv.fliped_state(self.state)
                    self.is_red_turn = False
                    self.turns = (int(self.args[6]) - 1) * 2 + 1
                else:
                    self.is_red_turn = True
                    self.turns = (int(self.args[6]) - 1) * 2
                if len(self.args) > 7 and self.args[7] == 'moves':
                    move_idx = 8
            elif self.args[0] == 'startpos':
                self.state = senv.INIT_STATE
                self.is_red_turn = True
                self.history = [self.state]
                self.turns = 0
                if len(self.args) > 1 and self.args[1] == 'moves':
                    move_idx = 2
            elif self.args[0] == 'moves':
                move_idx = 1
        else:
            self.state = senv.INIT_STATE
            self.is_red_turn = True
            self.history = [self.state]
            self.turns = 0
        logger.debug(f"state = {self.state}")
        # senv.render(self.state)
        # execute moves
        if move_idx != -1:
            for i in range(move_idx, len(self.args)):
                action = senv.parse_ucci_move(self.args[i])
                if not self.is_red_turn:
                    action = flip_move(action)
                self.history.append(action)
                self.state = senv.step(self.state, action)
                self.is_red_turn = not self.is_red_turn
                self.history.append(self.state)
            logger.debug(f"state = {self.state}")
            # senv.render(self.state)
    
    def cmd_fen(self):
        self.args.insert(0, 'position')
        self.cmd_position()


    def cmd_go(self):
        '''
        go ...
        　　让引擎根据内置棋盘的设置和设定的搜索方式来思考，有以下搜索方式可供选择(可以多选，直接跟在go后面)：
        　　❌(1) searchmoves <move1> .... <moven>，只让引擎在这几步中选择一步；
        　　❌(2) wtime <x>，白方剩余时间(单位是毫秒)；
        　　　     btime <x>，黑方剩余时间；
        　　　     winc <x>，白方每步增加的时间(适用于Fischer制)；
        　　　     binc <x>，黑方每步增加的时间；
        　　　     movestogo <x>，还有多少回合进入下一时段(适用于时段制)；
        　　这些选项用来设定时钟，它决定了引擎的思考时间；
        　　❌(3) ponder，让引擎进行后台思考(即对手在用时，引擎的时钟不起作用)；
        　　✅(4) depth <x>，指定搜索深度；
        　　❌(5) nodes <x>，指定搜索的节点数(即分析的局面数，一般它和时间成正比)；
        　　❌(6) mate <x>，在指定步数内只搜索杀棋；
        　　✅(7) movetime <x>，只花规定的时间搜索；
        　　✅(8) infinite，无限制搜索，直到杀棋。
        '''
        if not self.is_ready:
            return
        self.start_time = time()
        depth = None
        infinite = True
        self.remain_time = None
        self.model.close_pipes()
        self.pipe = self.model.get_pipes(need_reload=False)
        self.search_tree = defaultdict(VisitState)
        self.player = CChessPlayer(self.config, search_tree=self.search_tree, pipes=self.pipe,
                              enable_resign=False, debugging=True)
        for i in range(len(self.args)):
            if self.args[i] == 'depth':
                depth = int(self.args[i + 1])
                infinite = False
            if self.args[i] == 'movetime' or self.args[i] == 'time':
                self.remain_time = int(self.args[i + 1]) / 1000
            if self.args[i] == 'infinite':
                infinite = True
        logger.debug(f"depth = {depth}, infinite = {infinite}, remain_time = {self.remain_time}")
        search_worker = Thread(target=self.search_action, args=(depth, infinite))
        search_worker.daemon = True
        search_worker.start()
        
        if self.remain_time:
            t = Timer(self.remain_time - 0.01, self.cmd_stop)
            t.start()
        

    def cmd_stop(self):
        if not self.is_ready:
            return
        if self.player:
            no_act = None
            if self.state in self.history[:-1]:
                no_act = []
                for i in range(len(self.history) - 1):
                    if self.history[i] == self.state:
                        no_act.append(self.history[i + 1])
            action, value = self.player.close_and_return_action(self.state, self.turns, no_act)
            self.player = None
            self.model.close_pipes()
            self.info_best_move(action, value)
        else:
            logger.error(f"bestmove none")

    def cmd_quit(self):
        sys.exit()

    def load_model(self):
        self.model = CChessModel(self.config)
        if not load_best_model_weight(self.model):
            self.model.build()

    def search_action(self, depth, infinite):
        no_act = None
        if self.state in self.history[:-1]:
            no_act = []
            for i in range(len(self.history) - 1):
                if self.history[i] == self.state:
                    no_act.append(self.history[i + 1])
        action, _ = self.player.action(self.state, self.turns, no_act=no_act, depth=depth, infinite=infinite)
        _, value = self.player.debug[self.state]
        self.player.close(wait=False)
        self.player = None
        self.model.close_pipes()
        self.info_best_move(action, value)

    def info_best_move(self, action, value):
        self.end_time = time()
        score = int(value * 1000)
        print(f"info time {int((self.end_time - self.start_time) * 1000)} score {score}")
        # get ponder
        state = senv.step(self.state, action)
        ponder = None
        if state in self.search_tree:
            node = self.search_tree[state]
            cnt = 0
            for mov, action_state in node.a.items():
                if action_state.n > cnt:
                    ponder = mov
        if not self.is_red_turn:
            action = flip_move(action)
        action = senv.to_uci_move(action)
        output = f"bestmove {action}"
        if ponder:
            if self.is_red_turn:
                ponder = flip_move(ponder)
            ponder = senv.to_uci_move(ponder)
            output += f" ponder {ponder}"
        print(output)


if __name__ == "__main__":
    mp.freeze_support()
    sys.setrecursionlimit(10000)
    pwhc = PlayWithHumanConfig()
    pwhc.update_play_config(config.play)
    uci = UCI(config)
    uci.main()