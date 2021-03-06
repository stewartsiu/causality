import pandas as pd
import numpy as np
import statsmodels.api as sm
import scipy.stats
import itertools
from statsmodels.nonparametric.kernel_density import KDEMultivariateConditional, KDEMultivariate, EstimatorSettings
import pymc

DEFAULT_BINS = 2

class RobustRegressionTest():
    def __init__(self, y, x, z, data, alpha, variable_types={}):
        self.regression = sm.RLM(data[y], data[x+z])
        self.result = self.regression.fit()
        self.coefficient = self.result.params[x][0]
        confidence_interval = self.result.conf_int(alpha=alpha/2.)
        self.upper = confidence_interval[1][x][0]
        self.lower = confidence_interval[0][x][0]

    def independent(self):
        if self.coefficient > 0.:
            if self.lower > 0.:
                return False
            else:
                return True
        else:
            if self.upper < 0.:
                return False
            else:
                return True

class ChiSquaredTest():
    def __init__(self, y, x, z, data, alpha):
        self.alpha = alpha
        self.total_chi2 = 0.
        self.total_dof = 0
        for xi, yi in itertools.product(x,y):
            tables = data[[xi]+[yi]+z].copy()
            groupby_key = tuple([zi for zi in z] + [xi])
            tables = tables.join(pd.get_dummies(data[yi],prefix=yi)).groupby(groupby_key).sum()
            del tables[yi]

            z_values = {zi : data.groupby(zi).groups.keys() for zi in z}
            x_values = {xi : data.groupby(xi).groups.keys()}
            y_values = {yi : data.groupby(yi).groups.keys()}

            contingencies = itertools.product(*[z_values[zi] for zi in z])
            for contingency in contingencies:
                contingency_table = tables.loc[contingency].values
                try:
                    chi2, _, dof, _ = scipy.stats.chi2_contingency(contingency_table)
                except ValueError:
                    print "Potentially not enough data or entries with 0 present: Chi^2 Test not applicable."
                    chi2, _, dof, _ = scipy.stats.chi2_contingency(contingency_table+1) #Hack that shrinks towards equal distribution
                self.total_dof += dof
                self.total_chi2 += chi2
        self.total_p = 1. - scipy.stats.chi2.cdf(self.total_chi2, self.total_dof)

    def independent(self):
        if self.total_p < self.alpha:
            return False
        else:
            return True

class MixedChiSquaredTest(object):
    
    """
    This test compares the chi2 statistic between two distributions.  One where
    P(X,Y,Z) = P(X|Z)P(Y|Z)P(Z) (the conditionally indep distribution), where
    the samples are then discretized and chi2 is calculated, to the chi2 from
    just discretizing the original data.  

    If the chi2 from the original data is larger than the chi2 from the 
    conditionally independent data (with the appropriate p-value), then X and Y
    are deemed conditionally dependent given Z.
    """
    def __init__(self, y, x, z, X, alpha, variable_types={}, burn=1000, thin=10, bins={}):
        self.variable_types = variable_types
        self.bins = bins
        self.alpha = alpha
        self.x = x
        self.y = y
        self.z = z
        print '\nCreating indep test for x, y, z = ',x,y,z
        if len(X) > 300 or max(len(x+z),len(y+z)) >= 3:
            self.defaults=EstimatorSettings(n_jobs=4, efficient=True)
        else:
            self.defaults=EstimatorSettings(n_jobs=-1, efficient=False)
        self.densities = self.estimate_densities(x, y, z, X)
        self.N = len(X)
        self.mcmc_initialization = X[x+y+z].median().values
        self.burn = burn
        self.thin = thin
        self.null_df = self.generate_ci_sample()
        _, _, self.chi2_bound = self.discretize_and_get_chi2(self.null_df)
        self.chi2 = self.discretize_and_get_chi2(X)[1]
    
    def independent(self):
        if self.chi2 > self.chi2_bound:
            return False
        else:
            return True

    def discretize_and_get_chi2(self,X):
        discretized_df = self.discretize(X)
        f = lambda X : ChiSquaredTest(self.y, self.x, self.z, X, self.alpha).total_chi2
        lower, expected, upper = self.bootstrap(discretized_df, f, lower_confidence=self.alpha/2, upper_confidence=1.-self.alpha/2.)
        return lower, expected, upper

    def discretize(self, X):
        self.discretized = []
        discretized_X = X.copy()
        for column, var_type in self.variable_types.items():
            if column not in X:
                continue
            if var_type == 'c':
                bins = self.bins.get(column,DEFAULT_BINS)
                discretized_X[column] = pd.qcut(X[column],bins,labels=False)
                self.discretized.append(column)
        return discretized_X 

    def bootstrap(self, X, function, lower_confidence=.05/2., upper_confidence=1. - .05/2.):
        bootstrap_samples = self.N
        samples = []
        for i in xrange(bootstrap_samples):
            bs_indices = np.random.choice(xrange(len(X)), size=len(X), replace=True)
            sampled_arr = pd.DataFrame(X.values[bs_indices], columns=X.columns)
            samples.append(function(sampled_arr))
        samples = pd.DataFrame(samples)
        cis = samples.quantile([lower_confidence,upper_confidence])[0]
        lower_ci = cis[lower_confidence]
        expected = samples.mean()[0]
        upper_ci = cis[upper_confidence]
        return lower_ci, expected, upper_ci

    def estimate_densities(self, x, y, z, X):
        p_x_given_z = self.estimate_cond_pdf(x, z, X)
        p_y_given_z = self.estimate_cond_pdf(y, z, X)
        if len(z)==0:
            return p_x_given_z, p_y_given_z
        p_z = self.estimate_cond_pdf(z, [], X)
        return p_x_given_z, p_y_given_z, p_z

    def estimate_cond_pdf(self, x, z, X):
        # normal_reference works better with mixed types
        if 'c' not in [self.variable_types[xi] for xi in x+z]:
            bw = 'cv_ml'
        else:
            bw = 'cv_ml'#'normal_reference'
        # if conditioning on the empty set, return a pdf instead of cond pdf
        if len(z) == 0:
            if len(x)==0:
                raise Exception('x in P(x|z) cannot be null')
            return KDEMultivariate(X[x],
                                  var_type=''.join([self.variable_types[xi] for xi in x]),
                                  bw=bw,
                                  defaults=self.defaults)
        else:
            return KDEMultivariateConditional(endog=X[x],
                                              exog=X[z],
                                              dep_type=''.join([self.variable_types[xi] for xi in x]),
                                              indep_type=''.join([self.variable_types[zi] for zi in z]),
                                              bw=bw,
                                              defaults=self.defaults)

    def generate_ci_sample(self):
        x = self.x
        y = self.y
        z = self.z
        @pymc.stochastic(name='joint_sample')
        def ci_joint(value=self.mcmc_initialization):
            def logp(value):
                xi = [value[i] for i in range(len(x))]
                yi = [value[i+len(x)] for i in range(len(y))]
                zi = [value[i+len(x)+len(y)] for i in range(len(z))] 
                if len(z) == 0:
                    log_px_given_z = np.log(self.densities[0].pdf(data_predict=xi))
                    log_py_given_z = np.log(self.densities[1].pdf(data_predict=yi))
                    log_pz = 0.
                else:
                    log_px_given_z = np.log(self.densities[0].pdf(endog_predict=xi, exog_predict=zi))
                    log_py_given_z =np.log(self.densities[1].pdf(endog_predict=yi, exog_predict=zi))
                    log_pz = np.log(self.densities[2].pdf(data_predict=zi))
                return log_px_given_z + log_py_given_z + log_pz
        model = pymc.Model([ci_joint])
        mcmc = pymc.MCMC(model)
        burn = self.burn
        thin = self.thin
        samples = self.N
        iterations = samples * thin + burn
        mcmc.sample(iter=iterations, burn=burn, thin=thin)
        return pd.DataFrame(mcmc.trace('joint_sample')[:], columns=x+y+z)



if __name__=="__main__":
    y = ['x3']
    x = ['x1']
    z = ['x2']
    alpha = 0.05
    size = 5000
    x1 = np.random.normal(size=size)
    x2 = np.random.normal(size=size) + x1
    x3 = np.random.normal(size=size) + x2 
    X = pd.DataFrame({'x1':x1,'x2':x2, 'x3':x3})
    test = MixedChiSquaredTest(y, x, z, X, alpha, variable_types={'x1':'c', 'x2':'c', 'x3':'c'})
    print 'null', test.chi2_bound
    print 'actual', test.chi2
    print test.independent()
    raise Exception
    X_sampled = test.generate_ci_sample()
    print X.corr()
    print X_sampled.corr()
    regression = sm.RLM(X[y], X[x+z])
    result = regression.fit()
    print result.summary()
    regression = sm.RLM(X_sampled[y], X_sampled[x+z])
    result = regression.fit()
    print result.summary()
