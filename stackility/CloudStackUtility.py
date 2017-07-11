import boto3
import logging
import sys
import os
import time
import json
import yaml
import traceback

try:
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader


try:
    POLL_INTERVAL = os.environ.get('CSU_POLL_INTERVAL', 30)
except:
    POLL_INTERVAL = 30

logging.basicConfig(level=logging.INFO,
                    format='[%(levelname)s] %(asctime)s (%(module)s) %(message)s',
                    datefmt='%Y/%m/%d-%H:%M:%S')

logging.getLogger().setLevel(logging.INFO)


class CloudStackUtility:
    """
    Cloud stack utility is yet another tool create AWS Cloudformation stacks.
    """
    _b3Sess = None
    _cloudFormation = None
    _config = None
    _parameters = {}
    _stackParameters = []
    _s3 = None
    _tags = []
    _templateUrl = None
    _updateStack = False

    def __init__(self, config_block):
        """
        Cloud stack utility init method.

        Args:
            config_block - a dictionary creates from the CLI driver. See that
                           script for the things that are required and
                           optional.

        Returns:
           not a damn thing

        Raises:
            SystemError - if everything is'nt just right
        """
        if config_block:
            self._config = config_block
        else:
            logging.error('config block was garbage')
            raise SystemError

        if not self._initialize_session():
            logging.error('_intialize_session() was snafu')
            raise SystemError

        if not self._initialize_parameters():
            logging.error('_intialize_parameters() was snafu')
            raise SystemError

        if not self._initialize_tags():
            logging.error('_intialize_tags() was snafu')
            raise SystemError

        if not self._copy_stuff_to_S3():
            logging.error('_copy_stuff_to_S3() was snafu')
            raise SystemError

        if not self._set_update():
            logging.error('_set_update() was snafu')
            raise SystemError

    def wait_for_stack(self):
        logging.info('polling stack status, POLL_INTERVAL={}'.format(POLL_INTERVAL))
        time.sleep(POLL_INTERVAL)
        while True:
            try:
                response = self._cloudFormation.describe_stacks(StackName=self._config.get('stackName'))
                stack = response['Stacks'][0]
                current_status = stack['StackStatus']
                logging.info('Current status of ' + self._config.get('stackName') + ': ' + current_status)
                if current_status.endswith('COMPLETE') or current_status.endswith('FAILED'):
                    if current_status in ['CREATE_COMPLETE', 'UPDATE_COMPLETE']:
                        return True
                    else:
                        return False

                time.sleep(POLL_INTERVAL)
            except Exception as wtf:
                logging.error('Exception caught in wait_for_stack(): {}'.format(wtf))
                traceback.print_exc(file=sys.stdout)
                return False

    def create_stack(self):
        required_parameters = []
        self._stackParameters = []

        try:
            available_parameters = self._parameters.keys()
            if self._config.get('yaml'):
                with open(self._config.get('templateFile'), 'r') as f:
                    template = yaml.load(f, Loader=Loader)
            else:
                json_stuff = open(self._config.get('templateFile'))
                template = json.load(json_stuff)

            for parameter_name in template['Parameters']:
                required_parameters.append(str(parameter_name))

            logging.info(' required parameters: ' + str(required_parameters))
            logging.info('available parameters: ' + str(available_parameters))
            for required_parameter in required_parameters:
                parameter = {}
                parameter['ParameterKey'] = str(required_parameter)
                parameter['ParameterValue'] = self._parameters[str(required_parameter)]

            if self._config.get('dryrun', False):
                logging.info('This was a dryrun')
                sys.exit(0)
            else:
                logging.info('a journey of a thousand miles begins with a single step')

            if self._updateStack:
                self._tags.append({"Key": "CODE_VERSION_SD", "Value": self._config.get('codeVersion')})
                self._tags.append({"Key": "ANSWER", "Value": str(42)})
                stack = self._cloudFormation.update_stack(
                    StackName=self._config.get('stackName'),
                    TemplateURL=self._templateUrl,
                    Parameters=self._stackParameters,
                    Capabilities=['CAPABILITY_IAM', 'CAPABILITY_NAMED_IAM'],
                    Tags=self._tags
                )
            else:
                self._tags.append({"Key": "CODE_VERSION_SD", "Value": self._config.get('codeVersion')})
                self._tags.append({"Key": "ANSWER", "Value": str(42)})
                stack = self._cloudFormation.create_stack(
                    StackName=self._config.get('stackName'),
                    TemplateURL=self._templateUrl,
                    Parameters=self._stackParameters,
                    Capabilities=['CAPABILITY_IAM', 'CAPABILITY_NAMED_IAM'],
                    Tags=self._tags
                )
            logging.info(stack)
        except Exception as x:
            logging.error('Exception caught in create_stack(): {}'.format(x))
            traceback.print_exc(file=sys.stdout)

            return False

        return True

    def _initialize_parameters(self):
        with open(self._config.get('parameterFile')) as f:
            wrk = f.readline()
            while wrk:
                wrk = wrk.rstrip()
                key_val = wrk.split('=')
                if len(key_val) == 2:
                    self._parameters[key_val[0]] = key_val[1]

                wrk = f.readline()

        return True

    def _initialize_tags(self):
        with open(self._config.get('tagFile')) as f:
            wrk = f.readline()
            while wrk:
                tag = {}
                wrk = wrk.rstrip()
                key_val = wrk.split('=')
                if len(key_val) == 2:
                    tag['Key'] = key_val[0]
                    tag['Value'] = key_val[1]
                    self._tags.append(tag)

                wrk = f.readline()

        logging.info('Tags: {}'.format(json.dumps(
            self._tags,
            indent=4,
            sort_keys=True
        )))
        return True

    def _set_update(self):
        try:
            self._updateStack = False
            response = self._cloudFormation.describe_stacks(StackName=self._config.get('stackName'))
            stack = response['Stacks'][0]
            if stack['StackStatus'] in ['CREATE_COMPLETE', 'UPDATE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE']:
                self._updateStack = True
        except:
            self._updateStack = False

        logging.info('update_stack: ' + str(self._updateStack))
        return True

    def _copy_stuff_to_S3(self):
        try:
            stackfile_key, propertyfile_key = self._create_stack_file_keys()

            if not os.path.isfile(self._config.get('templateFile')):
                logging.info(self._config.get('templateFile') + " not actually a file")
                return False

            logging.info("Copying " +
                         self._config.get('parameterFile') +
                         " to " + "s3://" +
                         self._config.get('destinationBucket') +
                         "/" +
                         propertyfile_key)

            self._s3.upload_file(self._config.get('parameterFile'),
                                 self._config.get('destinationBucket'),
                                 propertyfile_key)

            logging.info("Copying " +
                         self._config.get('templateFile') +
                         " to " + "s3://" +
                         self._config.get('destinationBucket') +
                         "/" + stackfile_key)

            self._s3.upload_file(self._config.get('templateFile'),
                                 self._config.get('destinationBucket'),
                                 stackfile_key)

            self._templateUrl = 'https://s3.amazonaws.com/' + \
                self._config.get('destinationBucket') + \
                '/' + \
                stackfile_key

            logging.info("template_url: " + self._templateUrl)
            return True
        except Exception as x:
            logging.error('Exception caught in copy_stuff_to_S3(): {}'.format(x))
            traceback.print_exc(file=sys.stdout)
            return False

    def _initialize_session(self):
        try:
            if self._config.get('profile'):
                self._b3Sess = boto3.session.Session(profile_name=self._config.get('profile'))
            else:
                self._b3Sess = boto3.session.Session()

            self._s3 = self._b3Sess.client('s3')
            self._cloudFormation = self._b3Sess.client('cloudformation', region_name=self._config.get('region'))
            return True
        except Exception as wtf:
            logging.error('Exception caught in intialize_session(): {}'.format(wtf))
            traceback.print_exc(file=sys.stdout)
            return False

    def get_template_file(self):
        if self._config.get('templateFile'):
            return self._config.get('templateFile')
        else:
            return ''

    def _create_stack_file_keys(self):
        now = time.gmtime()
        stub = "templates/{stack_name}/{version}".format(
            stack_name=self._config.get('stackName'),
            version=self._config.get('codeVersion')
        )

        stub = stub + "/" + str(now.tm_year)
        stub = stub + "/" + str('%02d' % now.tm_mon)
        stub = stub + "/" + str('%02d' % now.tm_mday)
        stub = stub + "/" + str('%02d' % now.tm_hour)
        stub = stub + ":" + str('%02d' % now.tm_min)
        stub = stub + ":" + str('%02d' % now.tm_sec)
        template_key = stub + "/stack.json"
        property_key = stub + "/stack.properties"
        return template_key, property_key
