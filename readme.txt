人才服务一网通办事项编排与补偿平台
测试：python3 -m unittest discover -s tests -v
构建：python3 -m compileall -q one_stop_cases tests
验收：python3 -m one_stop_cases --database /tmp/one_stop_cases.sqlite3 run --fixture fixtures/acceptance.json --output /tmp/one_stop_cases-output.json
单测：python3 -m unittest tests.test_domain -v
