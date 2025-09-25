"""
Nonlinear Minimization with Futures Constraints

Extends WLS regression with constrained optimization when futures data is available.
Uses scipy.optimize to enforce forward price bounds from futures market.
"""

from typing import Any
import numpy as np
import polars as pl
from scipy.optimize import minimize
import scipy.optimize as opt

from .weight_least_square_regressor import WLSRegressor, Result


class NonlinearMinimization(WLSRegressor):
    """Constrained optimization for put-call parity with futures bounds."""
    
    def __init__(self, future_spread_mult: float = 0.0005, future_spread_threshold: float = 0.0020,
                 r_min: float = -0.05, r_max: float = 0.25, 
                 q_min: float = -0.30, q_max: float = 0.60):
        """
        Initialize with futures constraint parameters and rate bounds.
        
        Rate constraints are applied during optimization to ensure:
        - USD rates (r): -5% to +10% covers Fed funds, LIBOR/SOFR, and crypto lending  
        - BTC rates (q): -30% to +100% covers typical futures basis and funding rates
        
        Args:
            future_spread_mult: Additional spread buffer for constraints
            future_spread_threshold: Maximum allowed futures spread (as fraction of spot)
            r_min: Minimum USD interest rate (annualized, default: -0.05 = -5%)
            r_max: Maximum USD interest rate (annualized, default: +0.10 = +10%) 
            q_min: Minimum BTC funding rate (annualized, default: -0.30 = -30%)
            q_max: Maximum BTC funding rate (annualized, default: +1.00 = +100%)
        """
        super().__init__()
        self.future_spread_mult = future_spread_mult
        self.future_spread_threshold = future_spread_threshold
        self.r_min = r_min
        self.r_max = r_max
        self.q_min = q_min
        self.q_max = q_max

    def objective(self, params, X, y, weights):
        """Calculate weighted sum of squared residuals."""
        const, x1 = params
        residuals = y - (const + x1 * X[:, 1])
        return np.sum(weights * residuals**2)

    def rate_constraint_func(self, tau: float):
        """
        Create constraint functions for interest rate bounds.
        
        From regression parameters (const, coef), derives rates:
        r = -ln(coef)/tau, q = -ln(-const/S)/tau
        
        Args:
            tau: Time to expiry
            
        Returns:
            List of constraint dictionaries for scipy.optimize
        """
        constraints = []
        
        # r constraint: r_min <= -ln(coef)/tau <= r_max
        # Equivalent to: exp(-r_max*tau) <= coef <= exp(-r_min*tau)
        coef_min = np.exp(-self.r_max * tau)
        coef_max = np.exp(-self.r_min * tau)
        
        constraints.extend([
            {'type': 'ineq', 'fun': lambda params: params[1] - coef_min},  # coef >= coef_min
            {'type': 'ineq', 'fun': lambda params: coef_max - params[1]}   # coef <= coef_max
        ])
        
        # q constraint: q_min <= -ln(-const/S)/tau <= q_max
        # This is more complex since const depends on S, will be added in minimize_error
        
        return constraints

    def nonlinear_constraint_func(self, upper_bound: float, lower_bound: float):
        """
        Create constraint functions for forward price bounds.
        
        Constraint: lower_bound <= -const/coef <= upper_bound
        where F = -const/coef is the forward price
        """
        return [
            {'type': 'ineq', 'fun': lambda params: -params[0] / params[1] - lower_bound},
            {'type': 'ineq', 'fun': lambda params: upper_bound - (-params[0] / params[1])}
        ]

    def create_future_boundaries(self, best_bid_price: float, best_ask_price: float) -> tuple[float, float]:
        """
        Create constraint boundaries based on futures prices.
        
        Args:
            best_bid_price: Futures bid price
            best_ask_price: Futures ask price
            
        Returns:
            Tuple of (lower_bound, upper_bound)
        """
        mid_price = (best_bid_price + best_ask_price) / 2
        lb, ub = mid_price * np.array([1 - self.future_spread_mult / 2, 1 + self.future_spread_mult / 2])
        lower_bound = min(best_bid_price, lb)
        upper_bound = max(best_ask_price, ub)
        
        self.own_print(f"Constraint future bounds: {lower_bound:.2f} to {upper_bound:.2f} "
                      f"based on future price {best_bid_price:.2f} - {best_ask_price:.2f}")
        return lower_bound, upper_bound

    def check_if_future_too_wide(self, best_bid_price: float, best_ask_price: float, spot_price: float) -> bool:
        """Check if futures spread exceeds threshold."""
        spread = (best_ask_price - best_bid_price) / spot_price
        if spread > self.future_spread_threshold:
            self.own_print(f"Future spread is {spread:.4f}, threshold is {self.future_spread_threshold:.4f}")
            return True
        return False

    def fit(self, df: pl.DataFrame, prev_const: float, prev_coef: float) -> Result:
        """
        Fit constrained optimization with futures bounds when available.
        
        Args:
            df: Option synthetic data
            prev_const: Previous constant for warm start
            prev_coef: Previous coefficient for warm start
            
        Returns:
            Result dictionary with fitted parameters
        """
        if df.is_empty():
            raise ValueError("DataFrame is empty. Cannot fit model.")

        initial_guess = np.array([prev_const, prev_coef])
        lower_bound, upper_bound = None, None
        
        # Check if futures data is available and spread is acceptable
        if (df['bid_price_fut'].is_not_null().all() and 
            df['ask_price_fut'].is_not_null().all()):
            
            best_bid_price = df['bid_price_fut'][0]
            best_ask_price = df['ask_price_fut'][0]
            spot_price = df['S'][0]
            
            if not self.check_if_future_too_wide(best_bid_price, best_ask_price, spot_price):
                lower_bound, upper_bound = self.create_future_boundaries(best_bid_price, best_ask_price)
                return self.minimize_error(df, initial_guess, lower_bound, upper_bound, True)
            
            self.own_print("Future spread too wide, skip the constraint")
            return self.minimize_error(df, initial_guess, lower_bound, upper_bound, False)
        
        # Non-future expiry or no futures data
        self.own_print("Non-future expiry, use unconstrained optimization")
        return self.minimize_error(df, initial_guess, lower_bound, upper_bound, False)

    def minimize_error(self, df: pl.DataFrame, initial_guess: np.ndarray, 
                      lower_bound: float, upper_bound: float, use_constraints: bool) -> Result:
        """
        Perform the actual optimization with rate and futures constraints.
        
        Args:
            df: Option synthetic data
            initial_guess: Starting parameters
            lower_bound: Lower futures constraint bound
            upper_bound: Upper futures constraint bound
            use_constraints: Whether to apply futures constraints
            
        Returns:
            Result dictionary
        """
        y, X_with_const, weight = self.construct_inputs(df)
        tau = df["tau"][0]
        S = df["S"][0]
        
        # Always apply rate constraints
        constraints = self.rate_constraint_func(tau)
        
        # Add q constraints (more complex due to S dependency)
        # q_min <= -ln(-const/S)/tau <= q_max
        # Equivalent to: -S*exp(-q_max*tau) <= const <= -S*exp(-q_min*tau)  
        const_min = -S * np.exp(-self.q_max * tau)
        const_max = -S * np.exp(-self.q_min * tau)
        
        constraints.extend([
            {'type': 'ineq', 'fun': lambda params: params[0] - const_min},  # const >= const_min
            {'type': 'ineq', 'fun': lambda params: const_max - params[0]}   # const <= const_max
        ])
        
        # Add futures constraints if requested
        if use_constraints:
            futures_constraints = self.nonlinear_constraint_func(upper_bound, lower_bound)
            constraints.extend(futures_constraints)
            self.own_print(f"🎯 Applying rate constraints: r ∈ [{self.r_min:.3f}, {self.r_max:.3f}], q ∈ [{self.q_min:.3f}, {self.q_max:.3f}]")
            self.own_print(f"🎯 Applying futures constraints: F ∈ [{lower_bound:.2f}, {upper_bound:.2f}]")
        else:
            self.own_print(f"🎯 Applying rate constraints only: r ∈ [{self.r_min:.3f}, {self.r_max:.3f}], q ∈ [{self.q_min:.3f}, {self.q_max:.3f}]")
        
        result = minimize(
            fun=self.objective,
            x0=initial_guess,
            args=(X_with_const, y, weight),
            method='SLSQP',
            constraints=constraints
        )
        
        if not result.success:
            raise ValueError(f"Optimization failed: {result.message}")
        
        const, coef = result.x
        self.own_print("Optimization successful.")
        self.own_print(f"Optimal parameters (const, coef): {const:.6f}, {coef:.6f}")
        self.own_print(f"Optimal objective value (SSE): {result.fun:.4f}")
        
        # Calculate R-squared approximation
        residuals = y - (const + coef * X_with_const[:, 1])
        sse = np.sum(weight * residuals**2)
        y_weighted_mean = np.sum(weight * y) / np.sum(weight)
        sst = np.sum(weight * (y - y_weighted_mean)**2)
        r_squared = 1 - (sse / sst)
        
        return self.get_result_from_optimization(
            const, coef, S, tau, float(r_squared), float(sse)
        )