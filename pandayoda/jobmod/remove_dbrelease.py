# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
#
# Authors:
# - Taylor Childers (john.taylor.childers@cern.ch)
# - Paul Nilsson (paul.nilsson@cern.ch)


# removes '--DBRelease' from jobPars
def apply_mod(job_def):
    jobpars = job_def['jobPars']

    newpars = ''
    for par in jobpars.split():
        if not par.startswith('--DBRelease'):
            newpars += par + ' '

    job_def['jobPars'] = newpars
    return job_def
