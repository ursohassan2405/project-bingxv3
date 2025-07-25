# analysis/worker.py
"""Background worker for continuous technical analysis processing."""

import asyncio
import logging
from typing import Dict, List, Optional, Any, Set
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

from api.market_data import get_market_data_api
from database.connection import get_session
from database.repository import AssetRepository, IndicatorRepository, SignalRepository
from analysis.indicators import get_technical_indicators
from analysis.volume import get_volume_analyzer
from analysis.signals import get_signal_generator, SignalType
from config.trading_config import TradingConfig
from utils.logger import get_logger, trading_logger, performance_logger
from utils.worker_coordinator import get_coordinator
from api.web_api import manager as connection_manager

# Import test mode functions for aggressive testing
try:
    from api.web_api import is_test_mode_active, get_test_mode_config, increment_test_mode_stat
except ImportError:
    # Fallback functions if import fails
    def is_test_mode_active(): return False
    def get_test_mode_config(): return {}
    def increment_test_mode_stat(stat_name, increment=1): pass

logger = get_logger(__name__)


class AnalysisWorkerError(Exception):
    """Exception for analysis worker errors."""
    pass


class AnalysisWorker:
    """Background worker for continuous technical analysis."""
    
    def __init__(self):
        self.market_api = get_market_data_api()
        self.indicators = get_technical_indicators()
        self.volume_analyzer = get_volume_analyzer()
        self.signal_generator = get_signal_generator()
        self.asset_repo = AssetRepository()
        self.indicator_repo = IndicatorRepository()
        self.signal_repo = SignalRepository()
        self.config = TradingConfig()
        self.coordinator = get_coordinator()
        
        self.is_running = False
        self.analysis_tasks = {}
        self.executor = ThreadPoolExecutor(max_workers=self.config.MAX_WORKERS)
        self.worker_id = "analysis_worker"
        
        # Performance tracking
        self.analysis_stats = {
            'total_analyses': 0,
            'successful_analyses': 0,
            'failed_analyses': 0,
            'signals_generated': 0,
            'last_analysis_time': None,
            'average_analysis_time': 0.0,
        }
    
    async def start(self):
        """Start the analysis worker."""
        if self.is_running:
            logger.warning("Analysis worker is already running")
            return
        
        self.is_running = True
        logger.info("Starting analysis worker...")
        
        try:
            # Initialize the global client first
            from api.client import initialize_client
            logger.info("Initializing BingX client for analysis worker...")
            if not await initialize_client():
                raise RuntimeError("Failed to initialize BingX client")
            logger.info("BingX client initialized successfully for analysis worker")
            
            # Register with coordinator
            await self.coordinator.register_worker(self.worker_id, 'analysis')
            logger.info(f"Registered analysis worker with coordinator: {self.worker_id}")
            
            # Start main analysis loop
            await self._run_analysis_loop()
        except Exception as e:
            logger.error(f"Analysis worker error: {e}")
            self.is_running = False
            # Unregister from coordinator on error
            await self.coordinator.unregister_worker(self.worker_id)
            raise
    
    async def stop(self):
        """Stop the analysis worker."""
        if not self.is_running:
            return
        
        logger.info("Stopping analysis worker...")
        self.is_running = False
        
        # Cancel any running tasks
        for task in self.analysis_tasks.values():
            if not task.done():
                task.cancel()
        
        # Wait for tasks to complete
        if self.analysis_tasks:
            await asyncio.gather(*self.analysis_tasks.values(), return_exceptions=True)
        
        # Shutdown executor
        self.executor.shutdown(wait=True)
        
        # Unregister from coordinator
        await self.coordinator.unregister_worker(self.worker_id)
        logger.info(f"Unregistered analysis worker from coordinator: {self.worker_id}")
        
        logger.info("Analysis worker stopped")
    
    async def _run_analysis_loop(self):
        """Main analysis loop."""
        while self.is_running:
            try:
                loop_start = datetime.utcnow()
                
                # Get valid assets to analyze
                valid_assets = await self._get_valid_assets()
                
                if not valid_assets:
                    logger.warning("No valid assets found for analysis")
                    await asyncio.sleep(60)  # Wait 1 minute before retrying
                    continue
                
                logger.info(f"Starting analysis cycle for {len(valid_assets)} assets")
                
                # Perform analysis for all assets
                analysis_results = await self._analyze_all_assets(valid_assets)
                
                # Process results
                await self._process_analysis_results(analysis_results)
                
                # Calculate cycle time
                cycle_duration = (datetime.utcnow() - loop_start).total_seconds()
                
                # Log performance
                performance_logger.execution_time(
                    "analysis_cycle",
                    cycle_duration,
                    {
                        "assets_analyzed": len(valid_assets),
                        "successful_analyses": len([r for r in analysis_results if not r.get('error')]),
                        "signals_generated": len([r for r in analysis_results if r.get('signal', {}).get('signal_type') != SignalType.NEUTRAL.value]),
                    }
                )
                
                # Wait before next cycle (with test mode adjustments)
                base_interval = self.config.SCAN_INTERVAL_SECONDS
                
                # In test mode, run analysis cycles more frequently
                if is_test_mode_active():
                    test_config = get_test_mode_config()
                    # Reduce interval to 30% of normal in aggressive test mode
                    if test_config.get('aggressive_mode', False):
                        base_interval = int(base_interval * 0.3)
                        logger.info(f"🧪 TEST MODE: Reduced scan interval to {base_interval}s for aggressive testing")
                
                sleep_time = max(0, base_interval - cycle_duration)
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
                
            except Exception as e:
                logger.error(f"Error in analysis loop: {e}")
                if self.is_running:
                    await asyncio.sleep(30)  # Wait before retrying
    
    async def _get_valid_assets(self) -> List[Dict[str, Any]]:
        """Get list of valid assets for analysis."""
        try:
            with get_session() as session:
                assets = self.asset_repo.get_valid_assets(session)
                
                # Limit to configured maximum
                if len(assets) > self.config.MAX_ASSETS_TO_SCAN:
                    # Prioritize assets (you could implement priority logic here)
                    assets = assets[:self.config.MAX_ASSETS_TO_SCAN]
                
                return [{'symbol': asset.symbol, 'id': str(asset.id)} for asset in assets]
                
        except Exception as e:
            logger.error(f"Error getting valid assets: {e}")
            return []
    
    async def _analyze_all_assets(self, assets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Analyze all assets concurrently."""
        # Create analysis tasks
        tasks = []
        for asset in assets:
            task = asyncio.create_task(
                self._analyze_single_asset(asset['symbol'], asset['id'])
            )
            tasks.append(task)
        
        # Execute all analyses
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Process exceptions
        processed_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Error analyzing {assets[i]['symbol']}: {result}")
                processed_results.append({
                    'symbol': assets[i]['symbol'],
                    'asset_id': assets[i]['id'],
                    'error': str(result),
                    'timestamp': datetime.utcnow().isoformat(),
                })
            else:
                processed_results.append(result)
        
        return processed_results
    
    async def _analyze_single_asset(self, symbol: str, asset_id: str) -> Dict[str, Any]:
        """Analyze a single asset."""
        analysis_start = datetime.utcnow()
        
        try:
            # Fetch market data for all required timeframes
            candles_data = await self._fetch_candles_for_analysis(symbol)
            
            if not all(candles_data.values()):
                raise AnalysisWorkerError("Insufficient market data")
            
            # Calculate indicators for each timeframe
            indicators_by_timeframe = {}
            for timeframe, candles in candles_data.items():
                if candles:
                    try:
                        indicators = self.indicators.calculate_all_indicators(candles)
                        indicators_by_timeframe[timeframe] = indicators
                    except Exception as e:
                        logger.warning(f"Error calculating indicators for {symbol} {timeframe}: {e}")
                        indicators_by_timeframe[timeframe] = {}
            
            # Perform volume analysis
            volume_analysis = {}
            if candles_data.get('spot'):
                try:
                    volume_analysis = self.volume_analyzer.comprehensive_volume_analysis(
                        candles_data['spot'], symbol, 'spot'
                    )
                except Exception as e:
                    logger.warning(f"Error in volume analysis for {symbol}: {e}")
                    volume_analysis = {'error': str(e)}
            
            # Generate trading signal
            signal_result = {}
            try:
                signal_result = self.signal_generator.generate_trading_signal(
                    symbol,
                    candles_data.get('spot', []),
                    candles_data.get('2h', []),
                    candles_data.get('4h', [])
                )
            except Exception as e:
                logger.warning(f"Error generating signal for {symbol}: {e}")
                signal_result = {'error': str(e)}
            
            # Calculate analysis duration
            analysis_duration = (datetime.utcnow() - analysis_start).total_seconds()
            
            # Update statistics
            self.analysis_stats['total_analyses'] += 1
            self.analysis_stats['successful_analyses'] += 1
            self.analysis_stats['last_analysis_time'] = datetime.utcnow()
            
            # Update average analysis time
            if self.analysis_stats['average_analysis_time'] == 0:
                self.analysis_stats['average_analysis_time'] = analysis_duration
            else:
                self.analysis_stats['average_analysis_time'] = (
                    self.analysis_stats['average_analysis_time'] * 0.9 + 
                    analysis_duration * 0.1
                )
            
            # Count signals
            if (signal_result.get('signal_type', SignalType.NEUTRAL.value) != SignalType.NEUTRAL.value and
                signal_result.get('confidence', 0) >= 0.4):
                self.analysis_stats['signals_generated'] += 1
            
            return {
                'symbol': symbol,
                'asset_id': asset_id,
                'timestamp': datetime.utcnow().isoformat(),
                'analysis_duration_seconds': analysis_duration,
                'indicators': indicators_by_timeframe,
                'volume_analysis': volume_analysis,
                'signal': signal_result,
                'candles_count': {tf: len(candles) for tf, candles in candles_data.items()},
            }
            
        except Exception as e:
            logger.error(f"Error analyzing {symbol}: {e}")
            self.analysis_stats['total_analyses'] += 1
            self.analysis_stats['failed_analyses'] += 1
            
            return {
                'symbol': symbol,
                'asset_id': asset_id,
                'timestamp': datetime.utcnow().isoformat(),
                'error': str(e),
                'analysis_duration_seconds': (datetime.utcnow() - analysis_start).total_seconds(),
            }
    
    async def _fetch_candles_for_analysis(self, symbol: str) -> Dict[str, List[Dict[str, Any]]]:
        """Fetch candle data for all required timeframes."""
        timeframes = {
            'spot': self.config.SPOT_TIMEFRAME,
            '2h': '2h',
            '4h': '4h',
        }
        
        candles_data = {}
        
        # Fetch data for each timeframe
        for tf_name, tf_value in timeframes.items():
            try:
                # Request permission from coordinator before making API call
                await self.coordinator.request_api_permission(self.worker_id, 'market_data')
                
                # Determine limit based on indicator requirements
                if tf_name == 'spot':
                    limit = max(100, self.config.VOLUME_SPIKE_LOOKBACK + 20)  # Increased for correlation analysis
                else:
                    limit = max(50, self.config.CENTER_PERIOD + 10)  # For MA calculations
                
                candles = await self.market_api.get_candles(
                    symbol=symbol,
                    timeframe=tf_value,
                    limit=limit
                )
                
                candles_data[tf_name] = candles
                
            except Exception as e:
                logger.warning(f"Error fetching {tf_name} candles for {symbol}: {e}")
                candles_data[tf_name] = []
        
        return candles_data
    
    async def _process_analysis_results(self, results: List[Dict[str, Any]]):
        """Process and persist analysis results."""
        try:
            with get_session() as session:
                for result in results:
                    if result.get('error'):
                        continue  # Skip failed analyses
                    
                    try:
                        await self._persist_analysis_result(session, result)
                    except Exception as e:
                        logger.error(f"Error persisting result for {result.get('symbol', 'unknown')}: {e}")
            
            # Log summary
            successful_results = [r for r in results if not r.get('error')]
            signals_generated = [r for r in successful_results if r.get('signal', {}).get('signal_type') != SignalType.NEUTRAL.value]
            
            logger.info(f"Analysis cycle complete: {len(successful_results)}/{len(results)} successful, "
                       f"{len(signals_generated)} signals generated")
            
        except Exception as e:
            logger.error(f"Error processing analysis results: {e}")
    
    async def _persist_analysis_result(self, session, result: Dict[str, Any]):
        """Persist a single analysis result to database."""
        symbol = result['symbol']
        asset_id = result['asset_id']
        timestamp = datetime.fromisoformat(result['timestamp'].replace('Z', '+00:00'))
        
        # Persist indicators for each timeframe
        indicators_data = result.get('indicators', {})
        for timeframe, indicators in indicators_data.items():
            if not indicators:
                continue
            
            try:
                self.indicator_repo.upsert_indicators(
                    session,
                    asset_id=asset_id,
                    timeframe=timeframe,
                    timestamp=timestamp,
                    mm1=indicators.get('mm1'),
                    center=indicators.get('center'),
                    rsi=indicators.get('rsi'),
                    volume_sma=indicators.get('volume_sma'),
                    additional_data={
                        'analysis_duration': result.get('analysis_duration_seconds'),
                        'candles_analyzed': result.get('candles_count', {}).get(timeframe, 0),
                    }
                )
            except Exception as e:
                logger.warning(f"Error persisting indicators for {symbol} {timeframe}: {e}")
        
        # Persist signal if significant (with test mode adjustments)
        signal_data = result.get('signal', {})
        
        # Adjust persistence threshold based on test mode
        persistence_threshold = 0.3  # Default threshold
        is_test_mode = is_test_mode_active()
        
        if is_test_mode:
            test_config = get_test_mode_config()
            persistence_threshold = 0.1  # Much lower threshold in test mode
            
            # Force signals to be more aggressive in test mode
            if signal_data.get('signal_type', SignalType.NEUTRAL.value) == SignalType.NEUTRAL.value:
                # Sometimes force neutral signals to become actionable in test mode
                if test_config.get('force_signals', False) and result.get('indicators', {}):
                    indicators_spot = result.get('indicators', {}).get('spot', {})
                    mm1 = indicators_spot.get('mm1', 0)
                    center = indicators_spot.get('center', 0)
                    
                    if mm1 and center:
                        # Force a signal based on MM1 vs Center position
                        if mm1 > center:
                            signal_data = {
                                'signal_type': SignalType.BUY.value,
                                'confidence': 0.8,  # High confidence for test
                                'rules_triggered': ['test_mode_forced_buy'],
                                'timestamp': datetime.utcnow().isoformat(),
                                'test_mode_forced': True
                            }
                            logger.warning(f"🧪 TEST MODE: Forced BUY signal for {symbol}")
                        else:
                            signal_data = {
                                'signal_type': SignalType.SELL.value,
                                'confidence': 0.8,  # High confidence for test
                                'rules_triggered': ['test_mode_forced_sell'],
                                'timestamp': datetime.utcnow().isoformat(),
                                'test_mode_forced': True
                            }
                            logger.warning(f"🧪 TEST MODE: Forced SELL signal for {symbol}")
            
            # Boost signal confidence in test mode
            if signal_data.get('confidence', 0) > 0:
                boosted_confidence = min(0.95, signal_data.get('confidence', 0) + 0.3)
                signal_data['confidence'] = boosted_confidence
                logger.info(f"🧪 TEST MODE: Boosted signal confidence for {symbol} to {boosted_confidence}")
        
        if (signal_data.get('signal_type', SignalType.NEUTRAL.value) != SignalType.NEUTRAL.value and
            signal_data.get('confidence', 0) >= persistence_threshold):
            
            try:
                self.signal_repo.create_signal(
                    session,
                    asset_id=asset_id,
                    signal_type=signal_data['signal_type'],
                    strength=signal_data.get('confidence', 0),
                    rules_triggered=signal_data.get('rules_triggered', []),
                    indicators_snapshot={
                        'indicators': indicators_data,
                        'volume_analysis': result.get('volume_analysis', {}),
                        'analysis_metadata': {
                            'analysis_duration': result.get('analysis_duration_seconds'),
                            'candles_analyzed': result.get('candles_count', {}),
                        },
                        'test_mode_data': {
                            'is_test_mode': is_test_mode,
                            'forced_signal': signal_data.get('test_mode_forced', False),
                            'boosted_confidence': is_test_mode and signal_data.get('confidence', 0) > 0,
                            'test_config': get_test_mode_config() if is_test_mode else {}
                        }
                    }
                )
                
                # Broadcast the new signal to connected WebSocket clients
                signal_to_broadcast = {
                    "type": "new_signal",
                    "payload": {
                        "symbol": symbol,
                        "signal_type": signal_data['signal_type'],
                        "strength": signal_data.get('confidence', 0),
                        "timestamp": signal_data.get('timestamp', datetime.utcnow().isoformat()),
                        "rules_triggered": signal_data.get('rules_triggered', []),
                        "trading_recommendation": signal_data.get('trading_recommendation', {})
                    }
                }
                await connection_manager.broadcast(signal_to_broadcast)
                logger.info(f"Broadcasted new signal for {symbol}: {signal_data['signal_type']}")
                
                # Update test mode statistics if active
                if is_test_mode:
                    increment_test_mode_stat('signals_generated')
                    if signal_data.get('test_mode_forced', False):
                        increment_test_mode_stat('signals_forced')
                        logger.warning(f"🧪 TEST MODE: Forced signal statistics updated for {symbol}")

            except Exception as e:
                logger.warning(f"Error persisting signal for {symbol}: {e}")
    
    async def get_worker_status(self) -> Dict[str, Any]:
        """Get current worker status and statistics."""
        # Get coordinator stats
        coordinator_stats = await self.coordinator.get_coordinator_stats()
        
        return {
            'is_running': self.is_running,
            'worker_id': self.worker_id,
            'statistics': self.analysis_stats.copy(),
            'configuration': {
                'scan_interval_seconds': self.config.SCAN_INTERVAL_SECONDS,
                'max_assets_to_scan': self.config.MAX_ASSETS_TO_SCAN,
                'max_workers': self.config.MAX_WORKERS,
                'analysis_timeframes': self.config.ANALYSIS_TIMEFRAMES,
            },
            'performance': {
                'success_rate': (
                    self.analysis_stats['successful_analyses'] / 
                    max(self.analysis_stats['total_analyses'], 1) * 100
                ),
                'average_analysis_time': self.analysis_stats['average_analysis_time'],
                'signals_per_hour': (
                    self.analysis_stats['signals_generated'] / 
                    max((datetime.utcnow() - (self.analysis_stats['last_analysis_time'] or datetime.utcnow())).total_seconds() / 3600, 1)
                ) if self.analysis_stats['last_analysis_time'] else 0,
            },
            'coordination': {
                'coordinator_stats': coordinator_stats,
                'worker_registration': coordinator_stats.get('workers', {}).get(self.worker_id, 'Not registered'),
                'api_request_coordination': 'Enabled',
                'resource_allocation': '20% of rate limit budget'
            }
        }
    
    async def analyze_specific_symbol(self, symbol: str) -> Dict[str, Any]:
        """Analyze a specific symbol on demand."""
        try:
            # Get asset ID
            with get_session() as session:
                asset = self.asset_repo.get_by_symbol(session, symbol)
                if not asset:
                    raise AnalysisWorkerError(f"Asset {symbol} not found in database")
                
                asset_id = str(asset.id)
            
            # Perform analysis
            result = await self._analyze_single_asset(symbol, asset_id)
            
            # Persist result if successful
            if not result.get('error'):
                with get_session() as session:
                    await self._persist_analysis_result(session, result)
            
            logger.info(f"On-demand analysis completed for {symbol}")
            return result
            
        except Exception as e:
            logger.error(f"Error in on-demand analysis for {symbol}: {e}")
            return {
                'symbol': symbol,
                'timestamp': datetime.utcnow().isoformat(),
                'error': str(e),
            }


# Global analysis worker instance
_analysis_worker = None


def get_analysis_worker() -> AnalysisWorker:
    """Get the global AnalysisWorker instance."""
    global _analysis_worker
    if _analysis_worker is None:
        _analysis_worker = AnalysisWorker()
    return _analysis_worker


# Convenience functions
async def start_analysis_worker():
    """Start the global analysis worker."""
    worker = get_analysis_worker()
    await worker.start()


async def stop_analysis_worker():
    """Stop the global analysis worker."""
    worker = get_analysis_worker()
    await worker.stop()


async def get_worker_status() -> Dict[str, Any]:
    """Get status of the analysis worker."""
    worker = get_analysis_worker()
    return await worker.get_worker_status()


async def analyze_symbol_on_demand(symbol: str) -> Dict[str, Any]:
    """Analyze a specific symbol on demand."""
    worker = get_analysis_worker()
    return await worker.analyze_specific_symbol(symbol)


async def main():
    """Main entry point for running the analysis worker."""
    logger.info("Starting BingX Analysis Worker...")
    
    # Initialize database
    try:
        from database.connection import init_database
        await asyncio.get_event_loop().run_in_executor(None, init_database)
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        return
    
    # Start the worker
    worker = get_analysis_worker()
    
    try:
        await worker.start()
    except KeyboardInterrupt:
        logger.info("Shutdown signal received")
    except Exception as e:
        logger.error(f"Worker error: {e}")
    finally:
        await worker.stop()
        logger.info("Analysis worker shutdown complete")


if __name__ == "__main__":
    import signal
    
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, initiating shutdown...")
        # The main loop will handle the actual shutdown
        
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Run the worker
    asyncio.run(main())